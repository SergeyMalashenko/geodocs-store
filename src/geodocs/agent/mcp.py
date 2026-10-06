"""MCP-сервер статических инструментов агентного яруса.

Даёт LLM-агенту (Hermes) детерминированные инструменты вместо свободного
веб-поиска: поиск по порталам-донорам, скачивание файлов в inbox, чтение
HTML-страниц и проверку локальной базы. Отдельная группа read-only
инструментов (find_documents, document_files, read_document_text,
document_extractions) обслуживает Q&A поверх локальной базы — см. agent/ask.py.
Запуск — stdio:

    GEODOCS_AGENT_INBOX=<inbox> GEODOCS_HOME=<home> geodocs-agent-mcp

Зависимость `mcp` — extra: pip install 'geodocs[mcp]'. Импорт — ленивый.
Тесты вызывают impl-функции напрямую, без транспорта.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .portals import Candidate, DocQuery, PortalError, get_portal, list_portals

INBOX_ENV = "GEODOCS_AGENT_INBOX"
_MAX_CANDIDATES = 20
_MAX_TEXT_CHARS = 100_000
_PDFTOTEXT = shutil.which("pdftotext")


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
# Реализации инструментов (тестируются напрямую, без транспорта MCP)
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
    from ..store import DocumentStore

    store = DocumentStore(ctx.home / "geodocs.sqlite3", files_dir=ctx.home / "files")
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
# Read-only инструменты локальной базы (Q&A поверх скачанных документов)
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
    """Файлы версии: путь, название, размер — что читать через read_document_text."""
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
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        if _PDFTOTEXT is None:
            return {"error": "pdftotext не установлен (пакет poppler-utils)"}
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
            return {"error": f"pdftotext: {proc.stderr.strip()[:300]}"}
        text = proc.stdout
    elif suffix == ".docx":
        text = _docx_text(path)
    elif suffix in {".html", ".htm", ".txt"}:
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        return {"error": f"неподдерживаемый тип файла: {suffix or '(нет расширения)'}"}
    limit = max(1_000, min(max_chars, _MAX_TEXT_CHARS))
    truncated = len(text) > limit
    return {
        "path": str(path),
        "title": file.title,
        "text": text[:limit],
        "total_chars": len(text),
        "truncated": truncated,
    }


def document_extractions_impl(ctx: McpContext, version_id: int) -> dict[str, Any]:
    """Уже извлечённые структурированные фрагменты версии (таблицы ВРИ и др.)."""
    store = _open_store(ctx)
    try:
        records = store.extractions_for_version(version_id)
    finally:
        store.close()
    return {
        "extractions": [
            {
                "zone_code": record.zone_code,
                "kind": record.kind.value,
                "origin": record.origin.value,
                "extractor": record.extractor,
                "confidence": record.confidence,
                "payload": record.payload,
            }
            for record in records
        ]
    }


# ---------------------------------------------------------------------------
# Сборка сервера и точка входа
# ---------------------------------------------------------------------------


def build_server(ctx: McpContext) -> Any:
    """MCPServer/FastMCP с зарегистрированными инструментами над ctx."""
    server_class = _load_mcp_server_class()
    server = server_class("geodocs-agent")

    @server.tool(
        name="search_document",
        description=(
            "Поиск муниципального документа (ПЗЗ/генплан/ЗОУИТ) по реквизитам"
            " по всем подключённым порталам параллельно. Возвращает кандидатов"
            " с URL и тегом портала."
        ),
    )
    async def search_document(
        municipality: str,
        doc_type: str,
        number: str,
        version_date: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        return await search_document_impl(
            municipality, doc_type, number, version_date, title
        )

    @server.tool(
        name="download_document",
        description=(
            "Скачивает файл документа в inbox задачи. С portal=<портал> использует"
            " адаптер портала; без portal — прямое скачивание (HTML отклоняется)."
        ),
    )
    async def download_document(
        url: str, portal: str | None = None, file_name: str | None = None
    ) -> dict[str, Any]:
        return await download_document_impl(
            ctx, url, portal=portal, file_name=file_name
        )

    @server.tool(
        name="fetch_page",
        description=(
            "Читает HTML-страницу (муниципальный сайт без API): чистый текст"
            " и все ссылки на PDF/DOCX со страницы кладётся в inbox."
        ),
    )
    async def fetch_page(url: str) -> dict[str, Any]:
        return await fetch_page_impl(ctx, url)

    @server.tool(
        name="check_local_store",
        description=(
            "Проверяет локальную базу geodocs: есть ли версия документа уже"
            " скачанной. Вызывай ПЕРВЫМ, чтобы не искать то, что есть."
        ),
    )
    async def check_local_store(
        doc_type: str, number: str, version_date: str
    ) -> dict[str, Any]:
        return check_local_store_impl(ctx, doc_type, number, version_date)

    @server.tool(
        name="find_documents",
        description=(
            "Поиск документов в ЛОКАЛЬНОЙ базе geodocs по подстроке: номер,"
            " название или муниципалитет. Возвращает version_id для"
            " read_document_text/document_extractions."
        ),
    )
    async def find_documents(
        query: str, doc_type: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        return find_documents_impl(ctx, query, doc_type, limit)

    @server.tool(
        name="document_files",
        description="Список файлов версии документа (пути, названия, размеры).",
    )
    async def document_files(version_id: int) -> dict[str, Any]:
        return document_files_impl(ctx, version_id)

    @server.tool(
        name="read_document_text",
        description=(
            "Читает текст файла версии из локальной базы (PDF/DOCX/HTML)."
            " Для длинных документов — порциями через max_chars."
        ),
    )
    async def read_document_text(
        version_id: int, file_index: int = 0, max_chars: int = 12_000
    ) -> dict[str, Any]:
        return read_document_text_impl(ctx, version_id, file_index, max_chars)

    @server.tool(
        name="document_extractions",
        description=(
            "Готовые структурированные фрагменты версии (таблицы ВРИ и др.),"
            " извлечённые ранее. Проверяй ПЕРЕД чтением полного текста."
        ),
    )
    async def document_extractions(version_id: int) -> dict[str, Any]:
        return document_extractions_impl(ctx, version_id)

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
