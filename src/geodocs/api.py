"""Публичный API второго контура TerraLogicX.

Два метода скрывают от первого контура всю механику поиска, скачивания,
кэширования, чтения и извлечения данных из документов:

    acquire_documents(municipality, doc_type, number, ...)  → DocumentRef
    query_documents(documents, query, response_schema=None) → QueryResult

acquire_documents — cache-first по инварианту (не по решению LLM): версия
с fetch_status=downloaded возвращается из базы без внешнего поиска; промах
по известной версии уходит в статическую ступень (порталы по
document_sources: файлы карточек РГИС, полный текст cntd), затем —
в агентный ярус (run_task), после которого lookup повторяется.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import tempfile
from collections.abc import Awaitable, Callable
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from .agent.config import AgentTierConfig, load_config
from .agent.executors import build_executor
from .agent.portals import Candidate, PortalError
from .agent.portals.cntd import CntdAdapter
from .agent.portals.rgis import RgisAdapter
from .agent.query import QueryResult, query_documents
from .agent.runner import run_task
from .agent.tasks import AgentTask
from .models import DocType, DocumentRef, FetchStatus, SourceName
from .store import DocumentStore

_UNKNOWN_VERSION_DATE = "unknown"  # как в store.py: редакция без даты


class AcquireStatus(str, Enum):
    """Итог обеспечения наличия документа в локальном хранилище."""

    CACHED = "cached"
    ACQUIRED = "acquired"
    NOT_FOUND = "not_found"
    FAILED = "failed"


class AcquireResult(BaseModel):
    """Refs с version_id (handle для query_documents) + статус и предупреждения."""

    refs: list[DocumentRef] = Field(default_factory=list)
    status: AcquireStatus
    warnings: list[str] = Field(default_factory=list)


def _resolve_home(home: str | Path | None) -> Path:
    if home is None:
        return Path(os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs")))
    return Path(home)


def _lookup_versions(
    store: DocumentStore,
    municipality: str,
    doc_type: str,
    number: str | None,
    version_date: str | None,
) -> list[dict[str, object]]:
    """Версии по реквизитам: муниципалитет — подстрокой, остальное точно."""
    sql = (
        "SELECT v.id AS version_id, d.municipality, d.doc_type, d.number,"
        " v.version_date, COALESCE(v.title, d.title) AS title,"
        " v.fetch_status, v.source_provider"
        " FROM document_versions v JOIN documents d ON d.id = v.document_id"
        " WHERE d.municipality LIKE ? AND d.doc_type = ?"
    )
    params: list[object] = [f"%{municipality}%", doc_type]
    if number:
        sql += " AND d.number = ?"
        params.append(number)
    if version_date:
        sql += " AND v.version_date = ?"
        params.append(version_date)
    sql += " ORDER BY v.id"
    rows = store.connection.execute(sql, params).fetchall()
    return [dict(zip(row.keys(), row, strict=True)) for row in rows]


def _row_to_ref(row: dict[str, object]) -> DocumentRef:
    provider = row["source_provider"]
    try:
        source = SourceName(str(provider)) if provider else SourceName.MANUAL
    except ValueError:
        source = SourceName.MANUAL
    return DocumentRef(
        municipality=str(row["municipality"]),
        doc_type=DocType(str(row["doc_type"])),
        number=str(row["number"]),
        version_date=str(row["version_date"]),
        title=row["title"] if isinstance(row["title"], str) else None,
        source=source,
        version_id=int(row["version_id"]),  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Статическая ступень: порталы по document_sources версии (до агентного яруса)
# ---------------------------------------------------------------------------

StaticFetcher = Callable[[DocumentStore, int, str], Awaitable[int]]


async def _fetch_rgis_card(store: DocumentStore, version_id: int, object_id: str) -> int:
    """Документные файлы карточки РГИС → save_file; вернёт число сохранённых."""
    if not object_id.isdigit():
        raise PortalError(f"rgis: source_object_id {object_id!r} не похож на id карточки")
    adapter = RgisAdapter()
    try:
        downloads = await adapter.fetch_card_documents(int(object_id))
    finally:
        await adapter.aclose()
    for item in downloads:
        store.save_file(
            version_id,
            item.content,
            filename=item.title,
            title=item.title,
            section=item.section,
            source_url=item.uri,
            source_provider=SourceName.RGIS,
        )
    return len(downloads)


async def _fetch_cntd_text(store: DocumentStore, version_id: int, object_id: str) -> int:
    """Полный текст документа docs.cntd.ru → save_file; 0/1 сохранённых."""
    if object_id.startswith("http"):
        candidate = Candidate(url=object_id, title=object_id, portal="cntd")
    else:
        candidate = Candidate(
            url=f"https://docs.cntd.ru/document/{object_id}",
            title=f"CNTD {object_id}",
            portal="cntd",
            meta={"document_id": object_id},
        )
    adapter = CntdAdapter()
    try:
        with tempfile.TemporaryDirectory(prefix="geodocs-cntd-") as tmp:
            path = await adapter.fetch(candidate, Path(tmp))
            data, name = path.read_bytes(), path.name
    finally:
        await adapter.aclose()
    store.save_file(
        version_id,
        data,
        filename=name,
        title=name,
        section="cntd",
        source_url=candidate.url,
        source_provider=SourceName.CNTD,
    )
    return 1


_STATIC_FETCHERS: dict[str, StaticFetcher] = {
    SourceName.RGIS.value: _fetch_rgis_card,
    SourceName.CNTD.value: _fetch_cntd_text,
}


async def _static_acquire_async(
    store: DocumentStore, version_id: int, warnings: list[str]
) -> bool:
    """Статические порталы по document_sources версии; True = версия скачана."""
    for source, object_id in store.source_refs_for_version(version_id):
        fetcher = _STATIC_FETCHERS.get(source)
        if fetcher is None:
            continue
        try:
            saved = await fetcher(store, version_id, object_id)
        except Exception as exc:  # noqa: BLE001 — статика не роняет acquire
            warnings.append(
                f"static:{source} для версии {version_id} не сработал: {exc}"
            )
            continue
        if saved:
            return True
    return False


def _try_static_acquire(
    store: DocumentStore, rows: list[dict[str, object]], warnings: list[str]
) -> bool:
    """Детерминированная ступень между cache-miss и агентным ярусом.

    True — хотя бы одна известная версия скачана статическими порталами.
    """
    for row in reversed(rows):
        version_id = int(row["version_id"])  # type: ignore[arg-type]
        try:
            if asyncio.run(_static_acquire_async(store, version_id, warnings)):
                return True
        except RuntimeError as exc:  # вызов из работающего event loop
            warnings.append(f"статическая ступень пропущена: {exc}")
            return False
    return False


def acquire_documents(
    municipality: str,
    doc_type: str,
    number: str | None = None,
    version_date: str | None = None,
    title: str | None = None,
    *,
    home: str | Path | None = None,
    allow_agent: bool = True,
    config: AgentTierConfig | None = None,
) -> AcquireResult:
    """Обеспечивает наличие документа в локальном хранилище (cache-first).

    Хранилище проверяется всегда первым; дальше — статическая ступень по
    document_sources известной версии (файлы карточек РГИС, полный текст
    cntd) и только потом агентный ярус (внешний поиск, нужен номер
    документа). Неудача — не исключение, а статус not_found/failed
    с причиной в warnings.
    """
    home_path = _resolve_home(home)
    store = DocumentStore(
        home_path / "geodocs.sqlite3", files_dir=home_path / "files"
    )
    try:
        DocType(doc_type)  # валидация типа до любых записей в базу
        rows = _lookup_versions(store, municipality, doc_type, number, version_date)
        downloaded = [
            row
            for row in rows
            if row["fetch_status"] == FetchStatus.DOWNLOADED.value
        ]
        if downloaded:
            return AcquireResult(
                refs=[_row_to_ref(row) for row in downloaded],
                status=AcquireStatus.CACHED,
            )

        warnings: list[str] = []
        if rows:
            warnings.append("документ известен базе, но файл не скачан")
            if _try_static_acquire(store, rows, warnings):
                refs = [
                    _row_to_ref(row)
                    for row in _lookup_versions(
                        store, municipality, doc_type, number, version_date
                    )
                    if row["fetch_status"] == FetchStatus.DOWNLOADED.value
                ]
                return AcquireResult(refs=refs, status=AcquireStatus.ACQUIRED)
        if not allow_agent:
            return AcquireResult(
                status=AcquireStatus.NOT_FOUND,
                warnings=[*warnings, "внешний поиск отключён (allow_agent=False)"],
            )
        if number is None:
            return AcquireResult(
                status=AcquireStatus.NOT_FOUND,
                warnings=[*warnings, "для внешнего поиска нужен номер документа"],
            )

        if rows:
            target = rows[-1]
            task = AgentTask(
                version_id=int(target["version_id"]),  # type: ignore[arg-type]
                doc_type=str(target["doc_type"]),
                number=str(target["number"]),
                municipality=str(target["municipality"]),
                title=target["title"] if isinstance(target["title"], str) else None,
                issuer=None,
                version_date=str(target["version_date"]),
            )
        else:
            version_id = store.register_ref(
                DocumentRef(
                    municipality=municipality,
                    doc_type=DocType(doc_type),
                    number=number,
                    version_date=version_date,
                    title=title,
                    source=SourceName.HERMES_AGENT,
                )
            )
            task = AgentTask(
                version_id=version_id,
                doc_type=DocType(doc_type).value,
                number=number,
                municipality=municipality,
                title=title,
                issuer=None,
                version_date=version_date or _UNKNOWN_VERSION_DATE,
            )

        cfg = config or load_config(home_path)
        executors = {
            name: build_executor(dataclasses.replace(entry, home=home_path))
            for name, entry in cfg.executors.items()
        }
        chain = [executors[name] for name in cfg.chain if name in executors]
        if not chain:
            store.set_fetch_status(task.version_id, FetchStatus.SEARCH_FAILED)
            return AcquireResult(
                status=AcquireStatus.FAILED,
                warnings=["ни один исполнитель из chain не построен"],
            )

        result = run_task(
            task, store, executors=chain, config=cfg, geodocs_home=home_path
        )
        if result.status == "downloaded":
            rows = _lookup_versions(store, municipality, doc_type, number, version_date)
            refs = [
                _row_to_ref(row)
                for row in rows
                if row["fetch_status"] == FetchStatus.DOWNLOADED.value
            ]
            return AcquireResult(
                refs=refs, status=AcquireStatus.ACQUIRED, warnings=warnings
            )
        status = (
            AcquireStatus.NOT_FOUND
            if result.status == "not_found"
            else AcquireStatus.FAILED
        )
        # версия не должна оставаться pending: попытка состоялась
        store.set_fetch_status(
            task.version_id,
            FetchStatus.NOT_FOUND
            if result.status == "not_found"
            else FetchStatus.SEARCH_FAILED,
        )
        if result.error:
            warnings.append(result.error)
        return AcquireResult(status=status, warnings=warnings)
    finally:
        store.close()


__all__ = [
    "AcquireResult",
    "AcquireStatus",
    "QueryResult",
    "acquire_documents",
    "query_documents",
]
