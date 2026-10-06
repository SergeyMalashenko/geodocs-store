"""MCP-сервер агентного яруса: три детерминированных инструмента для LLM.

Агенту (Hermes) видны ровно три tools — вся механика поиска, скачивания,
регистрации и чтения зашита внутри них, модель принимает только
семантические решения:

    find_document    cache-first discovery: локальная база → внешние порталы
    import_document  внешний URL → локальный документ (скачивание,
                     HTML-дотягивание ссылок, гейт, регистрация в базе)
    read_document    чтение локального документа: карточка, готовые
                     extractions, релевантные фрагменты текста

Низкоуровневые блоки (search_document_impl, download_document_impl,
fetch_page_impl, read_document_text_impl и др.) остаются в модуле как
внутренние строительные кирпичи и для тестов — в build_server они не
регистрируются. Запуск — stdio:

    GEODOCS_AGENT_INBOX=<inbox> GEODOCS_HOME=<home> geodocs-agent-mcp

Зависимость `mcp` — extra: pip install 'geodocs[mcp]'. Импорт — ленивый.
Тесты вызывают impl-функции напрямую, без транспорта.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..models import DocType, DocumentRef, FileRecord, SourceName
from .gate import DOCUMENT_SUFFIXES, gate_pass, verify_files
from .portals import Candidate, DocQuery, PortalError, get_portal, list_portals

INBOX_ENV = "GEODOCS_AGENT_INBOX"
_MAX_CANDIDATES = 20
_MAX_TEXT_CHARS = 100_000
_PDFTOTEXT = shutil.which("pdftotext")
_UNKNOWN_VERSION_DATE = "unknown"  # как в store.py: редакция без даты
_ZONE_CODE_RE = re.compile(r"[А-ЯЁA-Z]{1,4}-\d{1,2}")


class McpMissingError(RuntimeError):
    """Пакет mcp не установлен."""


def _load_mcp_server_class() -> type:
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP  # mcp 1.x
        except ImportError as exc:
            raise McpMissingError(
                "пакет mcp не установлен: pip install 'geodocs[mcp]'"
            ) from exc
        return FastMCP
    return MCPServer


@dataclass
class McpContext:
    """Окружение инструментов: каталог inbox и домашний каталог geodocs."""

    inbox: Path
    home: Path


# ---------------------------------------------------------------------------
# Низкоуровневые блоки: порталы и скачивание (внутренние, тестируются напрямую)
# ---------------------------------------------------------------------------


async def search_document_impl(
    municipality: str,
    doc_type: str,
    number: str,
    version_date: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Поиск документа по всем зарегистрированным порталам параллельно.

    Ошибка одного адаптера не роняет остальных: она попадает в errors.
    """
    query = DocQuery(
        municipality=municipality,
        doc_type=doc_type,
        number=number,
        version_date=version_date,
        title=title,
    )
    names = list_portals()
    adapters = [get_portal(name) for name in names]
    gathered = await asyncio.gather(
        *[adapter.search(query) for adapter in adapters],
        return_exceptions=True,
    )
    candidates: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for name, result in zip(names, gathered):
        if isinstance(result, Exception):
            errors[name] = str(result)
            continue
        for candidate in result[:5]:
            candidates.append(
                {
                    "portal": candidate.portal,
                    "url": candidate.url,
                    "title": candidate.title,
                    "meta": candidate.meta,
                }
            )
    candidates.sort(key=lambda item: item["portal"])
    return {
        "candidates": candidates[:_MAX_CANDIDATES],
        "portals_queried": names,
        "errors": errors,
    }


async def download_document_impl(
    ctx: McpContext,
    url: str,
    portal: str | None = None,
    file_name: str | None = None,
) -> dict[str, Any]:
    """Скачивает файл по URL в inbox; с portal-адаптером или напрямую."""
    ctx.inbox.mkdir(parents=True, exist_ok=True)
    if portal:
        adapter = get_portal(portal)
        candidate = Candidate(url=url, title=file_name or url, portal=portal)
        path = await adapter.fetch(candidate, ctx.inbox)
        size = path.stat().st_size
        return {"path": str(path), "size": size, "content_type": None, "portal": portal}

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PortalError(f"не удалось скачать {url}: {exc}") from exc
    content_type = response.headers.get("content-type", "")
    if "html" in content_type.lower():
        raise PortalError(
            f"{url} отдал HTML-страницу, а не файл документа;"
            " ищи на странице прямую ссылку на PDF/DOCX"
        )
    name = (
        file_name or url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "document.bin"
    )
    path = ctx.inbox / name
    path.write_bytes(response.content)
    return {
        "path": str(path),
        "size": path.stat().st_size,
        "content_type": content_type,
    }


async def fetch_page_impl(ctx: McpContext, url: str) -> dict[str, Any]:
    """Читает HTML-страницу: текст в .txt, найденные файлы — в inbox."""
    ctx.inbox.mkdir(parents=True, exist_ok=True)
    adapter = get_portal("municipal")
    candidate = Candidate(url=url, title=url, portal="municipal")
    path = await adapter.fetch(candidate, ctx.inbox)
    preview = ""
    if path.suffix == ".txt":
        preview = path.read_text(encoding="utf-8", errors="replace")[:2000]
    files = sorted(p.name for p in ctx.inbox.iterdir() if p.is_file())
    return {
        "result_path": str(path),
        "text_preview": preview,
        "inbox_files": files,
    }


def check_local_store_impl(
    ctx: McpContext,
    doc_type: str,
    number: str,
    version_date: str,
) -> dict[str, Any]:
    """Статус версии в локальной базе: чтобы не искать то, что уже есть."""
    store = _open_store(ctx)
    try:
        rows = store.connection.execute(
            "SELECT v.id, v.fetch_status, v.source_provider, v.file_path,"
            " v.source_url, d.municipality, d.number, d.doc_type, v.version_date"
            " FROM document_versions v JOIN documents d ON d.id = v.document_id"
            " WHERE d.doc_type = ? AND d.number = ? AND v.version_date = ?"
            " ORDER BY v.id",
            (doc_type, number, version_date),
        ).fetchall()
    finally:
        store.close()
    if not rows:
        return {"known": False}
    versions = [
        {
            "version_id": row["id"],
            "municipality": row["municipality"],
            "fetch_status": row["fetch_status"],
            "source_provider": row["source_provider"],
            "file_path": row["file_path"],
            "source_url": row["source_url"],
        }
        for row in rows
    ]
    return {"known": True, "versions": versions}


# ---------------------------------------------------------------------------
# Низкоуровневые блоки: чтение локальной базы
# ---------------------------------------------------------------------------


def _open_store(ctx: McpContext) -> Any:
    from ..store import DocumentStore

    return DocumentStore(ctx.home / "geodocs.sqlite3", files_dir=ctx.home / "files")


def find_documents_impl(
    ctx: McpContext,
    query: str,
    doc_type: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Поиск версий в локальной базе по подстроке: номер, название, муниципалитет."""
    store = _open_store(ctx)
    try:
        like = f"%{query}%"
        sql = (
            "SELECT v.id, d.municipality, d.doc_type, d.number, v.version_date,"
            " d.title, v.fetch_status FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id"
            " WHERE (d.number LIKE ? OR d.title LIKE ? OR d.municipality LIKE ?)"
        )
        params: list[Any] = [like, like, like]
        if doc_type:
            sql += " AND d.doc_type = ?"
            params.append(doc_type)
        sql += " ORDER BY v.id LIMIT ?"
        params.append(max(1, min(limit, 50)))
        rows = store.connection.execute(sql, params).fetchall()
    finally:
        store.close()
    return {
        "versions": [
            {
                "version_id": row["id"],
                "municipality": row["municipality"],
                "doc_type": row["doc_type"],
                "number": row["number"],
                "version_date": row["version_date"],
                "title": row["title"],
                "fetch_status": row["fetch_status"],
            }
            for row in rows
        ]
    }


def document_files_impl(ctx: McpContext, version_id: int) -> dict[str, Any]:
    """Файлы версии: путь, название, размер — что читать через read_document."""
    store = _open_store(ctx)
    try:
        files = store.files_for_version(version_id)
    finally:
        store.close()
    return {
        "files": [
            {
                "index": index,
                "path": file.file_path,
                "title": file.title,
                "size": file.size_bytes,
            }
            for index, file in enumerate(files)
        ]
    }


_XML_TAG_RE = re.compile(r"<[^>]+>")


def _docx_text(path: Path) -> str:
    """Плоский текст DOCX: word/document.xml без тегов, абзацы — переводы строк."""
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    xml = xml.replace("</w:p>", "\n")
    return _XML_TAG_RE.sub("", xml)


def _file_text(path: Path) -> str:
    """Полный текст файла: pdf → pdftotext -layout, docx/html/txt — напрямую.

    ValueError с причиной — для неподдерживаемых типов и ошибок конвертации.
    """
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        if _PDFTOTEXT is None:
            raise ValueError("pdftotext не установлен (пакет poppler-utils)")
        proc = subprocess.run(
            [_PDFTOTEXT, "-layout", str(path), "-"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError(f"pdftotext: {proc.stderr.strip()[:300]}")
        return proc.stdout
    if suffix == ".docx":
        return _docx_text(path)
    if suffix in {".html", ".htm", ".txt"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"неподдерживаемый тип файла: {suffix or '(нет расширения)'}")


def read_document_text_impl(
    ctx: McpContext,
    version_id: int,
    file_index: int = 0,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    """Текст файла версии: pdf → pdftotext -layout, docx/html/txt — напрямую."""
    store = _open_store(ctx)
    try:
        files = store.files_for_version(version_id)
    finally:
        store.close()
    if not files:
        return {"error": f"у версии {version_id} нет файлов"}
    if file_index < 0 or file_index >= len(files):
        return {"error": f"file_index {file_index} вне диапазона 0..{len(files) - 1}"}
    file = files[file_index]
    path = Path(file.file_path)
    if not path.is_file():
        return {"error": f"файл не найден на диске: {path}"}
    try:
        text = _file_text(path)
    except ValueError as exc:
        return {"error": str(exc)}
    limit = max(1_000, min(max_chars, _MAX_TEXT_CHARS))
    truncated = len(text) > limit
    return {
        "path": str(path),
        "title": file.title,
        "text": text[:limit],
        "total_chars": len(text),
        "truncated": truncated,
    }


def search_document_text_impl(
    ctx: McpContext,
    version_id: int,
    pattern: str,
    file_index: int | None = None,
    context_chars: int = 1500,
    max_hits: int = 10,
) -> dict[str, Any]:
    """Поиск regex по полному тексту файлов версии с номерами страниц.

    Страницы — по форм-фидам pdftotext (\\f); одностраничный текст → page=None.
    Невалидный regex деградирует до литеральной строки.
    """
    store = _open_store(ctx)
    try:
        files = store.files_for_version(version_id)
    finally:
        store.close()
    if not files:
        return {"error": f"у версии {version_id} нет файлов"}
    if file_index is not None and (file_index < 0 or file_index >= len(files)):
        return {"error": f"file_index {file_index} вне диапазона 0..{len(files) - 1}"}
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    indices = [file_index] if file_index is not None else list(range(len(files)))
    hits: list[dict[str, Any]] = []
    searched = 0
    limit = max(1, min(max_hits, 50))
    for index in indices:
        file = files[index]
        path = Path(file.file_path)
        if not path.is_file():
            continue
        try:
            text = _file_text(path)
        except ValueError:
            continue
        searched += 1
        pages = text.split("\f")
        single_page = len(pages) == 1
        for page_number, page_text in enumerate(pages, start=1):
            for match in regex.finditer(page_text):
                start = max(0, match.start() - context_chars // 2)
                end = min(len(page_text), match.end() + context_chars // 2)
                hits.append(
                    {
                        "file_index": index,
                        "file": file.title or path.name,
                        "page": None if single_page else page_number,
                        "snippet": page_text[start:end].strip(),
                    }
                )
                if len(hits) >= limit:
                    return {"hits": hits, "files_searched": searched}
    return {"hits": hits, "files_searched": searched}


def document_extractions_impl(ctx: McpContext, version_id: int) -> dict[str, Any]:
    """Уже извлечённые структурированные фрагменты версии (таблицы ВРИ и др.)."""
    store = _open_store(ctx)
    try:
        records = store.extractions_for_version(version_id)
    finally:
        store.close()
    return {"extractions": [_serialize_extraction(record) for record in records]}


def _serialize_extraction(record: Any) -> dict[str, Any]:
    return {
        "zone_code": record.zone_code,
        "kind": record.kind.value,
        "origin": record.origin.value,
        "extractor": record.extractor,
        "confidence": record.confidence,
        "payload": record.payload,
    }


# ---------------------------------------------------------------------------
# Инструменты агентного яруса: find_document / import_document / read_document
# ---------------------------------------------------------------------------


async def find_document_impl(
    ctx: McpContext,
    municipality: str,
    doc_type: str | None = None,
    number: str | None = None,
    version_date: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Cache-first discovery: локальная база, при промахе — внешние порталы.

    Локальное попадание = версия с fetch_status=downloaded: дальше ничего не
    ищем, документ читается через read_document. Известная, но не скачанная
    версия — промах: выполняется внешний поиск (нужны doc_type и number).
    """
    store = _open_store(ctx)
    try:
        sql = (
            "SELECT v.id, d.municipality, d.doc_type, d.number, v.version_date,"
            " COALESCE(v.title, d.title) AS title, v.fetch_status"
            " FROM document_versions v JOIN documents d ON d.id = v.document_id"
            " WHERE d.municipality LIKE ?"
        )
        params: list[Any] = [f"%{municipality}%"]
        if doc_type:
            sql += " AND d.doc_type = ?"
            params.append(doc_type)
        if number:
            sql += " AND d.number = ?"
            params.append(number)
        if version_date:
            sql += " AND v.version_date = ?"
            params.append(version_date)
        sql += " ORDER BY v.id LIMIT 20"
        rows = store.connection.execute(sql, params).fetchall()
    finally:
        store.close()
    versions = [
        {
            "version_id": row["id"],
            "municipality": row["municipality"],
            "doc_type": row["doc_type"],
            "number": row["number"],
            "version_date": row["version_date"],
            "title": row["title"],
            "fetch_status": row["fetch_status"],
        }
        for row in rows
    ]
    if any(version["fetch_status"] == "downloaded" for version in versions):
        return {"local": True, "versions": versions}
    if doc_type and number:
        external = await search_document_impl(
            municipality, doc_type, number, version_date or "", title
        )
        return {"local": False, "versions": versions, **external}
    return {
        "local": False,
        "versions": versions,
        "note": "внешний поиск требует doc_type и number",
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def import_document_impl(
    ctx: McpContext,
    url: str,
    portal: str | None = None,
    doc_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Внешний источник → локальный документ: скачивание, гейт, регистрация.

    URL файла скачивается напрямую или через portal-адаптер; HTML-страница
    обрабатывается как оглавление: с неё дотягиваются файлы документа.
    Валидные файлы копируются в хранилище и регистрируются (record_agent_fetch);
    doc_hint несёт реквизиты (municipality, doc_type, number, version_date,
    title) для версий, которых ещё нет в базе.
    """
    ctx.inbox.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in ctx.inbox.iterdir() if p.is_file()}
    try:
        result = await download_document_impl(ctx, url, portal=portal)
        downloaded = [Path(result["path"])]
    except PortalError as exc:
        if "HTML" not in str(exc):
            return {"error": str(exc)}
        try:
            await fetch_page_impl(ctx, url)
        except (PortalError, httpx.HTTPError) as page_exc:
            return {"error": f"{exc}; страница тоже не открылась: {page_exc}"}
        after = {p.name for p in ctx.inbox.iterdir() if p.is_file()}
        downloaded = [
            ctx.inbox / name
            for name in sorted(after - before)
            if (ctx.inbox / name).suffix.casefold() in DOCUMENT_SUFFIXES
        ]
    if not downloaded:
        return {"error": "по ссылке не найдено файлов документа (PDF/DOCX/...)"}
    if not doc_hint:
        return {
            "error": "для регистрации в базе нужен doc_hint"
            " (municipality, doc_type, number)",
            "inbox_files": [str(path) for path in downloaded],
        }
    try:
        hint_type = DocType(doc_hint["doc_type"])
    except (KeyError, ValueError) as exc:
        return {"error": f"невалидный doc_hint: {exc}"}
    store = _open_store(ctx)
    try:
        version = store.find_version(
            municipality=doc_hint.get("municipality", ""),
            doc_type=hint_type,
            number=doc_hint.get("number", ""),
            version_date=doc_hint.get("version_date") or _UNKNOWN_VERSION_DATE,
        )
        if version is None:
            version_id = store.register_ref(
                DocumentRef(
                    municipality=doc_hint.get("municipality", ""),
                    doc_type=hint_type,
                    number=doc_hint.get("number", ""),
                    version_date=doc_hint.get("version_date"),
                    title=doc_hint.get("title"),
                    issuer=doc_hint.get("issuer"),
                    region_code=doc_hint.get("region_code"),
                    source=SourceName.HERMES_AGENT,
                    source_object_id=url,
                )
            )
        else:
            version_id = version.id
        target_dir = (
            store.files_dir
            / str(doc_hint.get("municipality", "")).replace(" ", "_")
            / f"{hint_type.value}_{str(doc_hint.get('number', '')).replace('/', '_').replace('-', '_')}"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for path in downloaded:
            destination = target_dir / path.name
            shutil.copy2(path, destination)
            copied.append(destination)
        verdicts = verify_files(copied)
        if not gate_pass(verdicts):
            reasons = "; ".join(
                f"{Path(v.path).name}: {v.reason}" for v in verdicts if not v.ok
            )
            return {
                "error": f"gate_rejected: {reasons or 'нет валидных файлов'}",
                "verdicts": [v.model_dump() for v in verdicts],
            }
        store.record_agent_fetch(
            version_id,
            files=[
                FileRecord(
                    path=str(path),
                    size=path.stat().st_size,
                    sha256=_sha256(path),
                    title=path.name,
                )
                for path in copied
            ],
            source_url=url,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source_provider=SourceName.HERMES_AGENT,
        )
    finally:
        store.close()
    return {
        "version_id": version_id,
        "files": [
            {"path": str(path), "size": path.stat().st_size, "title": path.name}
            for path in copied
        ],
        "fetch_status": "downloaded",
    }


def read_document_impl(
    ctx: McpContext,
    version_id: int,
    query: str | None = None,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    """Универсальное чтение локального документа.

    Без query — карточка версии, список файлов, все готовые extractions и
    начало первого файла. С query — extractions, релевантные запросу (по
    кодам зон), плюс фрагменты полного текста по ключевым словам запроса.
    Готовые extractions всегда проверяются раньше полного текста.
    """
    store = _open_store(ctx)
    try:
        row = store.connection.execute(
            "SELECT v.id, d.municipality, d.doc_type, d.number, v.version_date,"
            " COALESCE(v.title, d.title) AS title, v.fetch_status, v.source_url"
            " FROM document_versions v JOIN documents d ON d.id = v.document_id"
            " WHERE v.id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            return {"error": f"версия {version_id} не найдена в локальной базе"}
        files = store.files_for_version(version_id)
        extractions = store.extractions_for_version(version_id)
    finally:
        store.close()
    result: dict[str, Any] = {
        "version": {
            "version_id": row["id"],
            "municipality": row["municipality"],
            "doc_type": row["doc_type"],
            "number": row["number"],
            "version_date": row["version_date"],
            "title": row["title"],
            "fetch_status": row["fetch_status"],
            "source_url": row["source_url"],
        },
        "files": [
            {
                "index": index,
                "path": file.file_path,
                "title": file.title,
                "size": file.size_bytes,
            }
            for index, file in enumerate(files)
        ],
    }
    if query is None:
        result["extractions"] = [
            _serialize_extraction(record) for record in extractions
        ]
        preview = read_document_text_impl(ctx, version_id, 0, max_chars)
        result["text_preview"] = preview if "error" not in preview else None
        return result

    zones = {code.upper() for code in _ZONE_CODE_RE.findall(query.upper())}
    matched = [
        record
        for record in extractions
        if not zones or record.zone_code.upper() in zones
    ]
    result["extractions"] = [_serialize_extraction(record) for record in matched]
    words = [w for w in re.split(r"[^\w-]+", query, flags=re.UNICODE) if len(w) >= 3]
    if words:
        pattern = "|".join(re.escape(word) for word in words[:8])
        search = search_document_text_impl(
            ctx, version_id, pattern, context_chars=1200, max_hits=8
        )
        result["fragments"] = search.get("hits", [])
    else:
        result["fragments"] = []
    result["note"] = (
        "extractions — готовые структурированные фрагменты (приоритет);"
        " fragments — места полного текста по ключевым словам запроса."
        " Если ответа нет — уточни query или вызови read_document без него."
    )
    return result


# ---------------------------------------------------------------------------
# Сборка сервера и точка входа
# ---------------------------------------------------------------------------


def build_server(ctx: McpContext) -> Any:
    """MCPServer/FastMCP с тремя инструментами агентного яруса над ctx."""
    server_class = _load_mcp_server_class()
    server = server_class("geodocs-agent")

    @server.tool(
        name="find_document",
        description=(
            "Cache-first поиск муниципального документа (ПЗЗ/генплан/ЗОУИТ):"
            " сначала локальная база geodocs (версия с fetch_status=downloaded"
            " сразу готова к read_document), при промахе — внешние порталы"
            " (кандидаты с URL). Вызывай ПЕРВЫМ, чтобы не искать то, что есть."
        ),
    )
    async def find_document(
        municipality: str,
        doc_type: str | None = None,
        number: str | None = None,
        version_date: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        return await find_document_impl(
            ctx, municipality, doc_type, number, version_date, title
        )

    @server.tool(
        name="import_document",
        description=(
            "Превращает внешний URL (файл или HTML-страница со ссылками) в"
            " локальный документ: скачивает, проверяет и регистрирует в базе."
            " doc_hint: municipality, doc_type, number (+version_date, title)."
            " Возвращает version_id для read_document."
        ),
    )
    async def import_document(
        url: str,
        portal: str | None = None,
        doc_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await import_document_impl(ctx, url, portal=portal, doc_hint=doc_hint)

    @server.tool(
        name="read_document",
        description=(
            "Читает документ локальной базы по version_id. Без query — карточка,"
            " файлы, готовые extractions и начало текста. С query — релевантные"
            " extractions и фрагменты текста с номерами страниц."
        ),
    )
    async def read_document(
        version_id: int, query: str | None = None, max_chars: int = 12_000
    ) -> dict[str, Any]:
        return read_document_impl(ctx, version_id, query, max_chars)

    return server


def context_from_env() -> McpContext:
    inbox = Path(os.environ.get(INBOX_ENV, "inbox"))
    home = Path(os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs")))
    return McpContext(inbox=inbox, home=home)


def main() -> None:
    """Точка входа geodocs-agent-mcp: stdio-сервер."""
    server = build_server(context_from_env())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
