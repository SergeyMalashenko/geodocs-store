"""Публичный MCP-сервер второго контура: geodocs-mcp.

Ровно два инструмента — обёртки над публичным Python API (geodocs.api):

    acquire_documents  обеспечить наличие документа в хранилище:
                       cache-first → статические порталы (rgis/cntd по
                       document_sources) → агентный ярус (LLM, медленно)
    query_documents    семантический запрос к версиям документов:
                       статический fast-path по extractions (static:vri /
                       static:zouit) → LLM-агент (медленно)

Оба вызова потенциально долгие (агентный ярус — минуты): выполняются через
asyncio.to_thread, клиенту нужен большой read-timeout. База —
$GEODOCS_HOME/geodocs.sqlite3. Зависимость `mcp` — extra:
pip install 'geodocs[mcp]'. Запуск:

    geodocs-mcp [--transport stdio|streamable-http] [--host ...] [--port 8006]
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .api import acquire_documents as _acquire_documents
from .api import query_documents as _query_documents

INSTRUCTIONS = (
    "Второй контур geodocs. acquire_documents обеспечивает наличие"
    " муниципального документа (ПЗЗ, генплан, ЗОУИТ) в локальном хранилище:"
    " cache-first, затем статические порталы, затем агентный ярус (LLM,"
    " медленно — минуты). query_documents отвечает на запрос по version_ids"
    " из acquire_documents: сначала детерминированный fast-path по готовым"
    " extractions, промах — LLM-агент (медленно)."
)


def _load_server_class() -> tuple[type, bool]:
    """Класс MCP-сервера и флаг mcp>=2; без пакета mcp — McpMissingError."""
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
    except ImportError:
        from .agent.mcp import _load_mcp_server_class  # mcp 1.x → FastMCP

        return _load_mcp_server_class(), False
    return MCPServer, True


def create_server(
    *,
    home: str | Path | None = None,
    name: str = "geodocs",
    host: str = "127.0.0.1",
    port: int = 8006,
    streamable_http_path: str = "/mcp",
    stateless_http: bool = True,
    json_response: bool = True,
) -> Any:
    """MCPServer/FastMCP с двумя инструментами-обёртками над api.py."""
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not streamable_http_path.startswith("/"):
        raise ValueError("streamable_http_path must start with '/'")
    server_class, mcp2 = _load_server_class()
    if mcp2:
        server = server_class(name, version=__version__, instructions=INSTRUCTIONS)
    else:
        server = server_class(
            name,
            instructions=INSTRUCTIONS,
            host=host,
            port=port,
            streamable_http_path=streamable_http_path,
            stateless_http=stateless_http,
            json_response=json_response,
        )

    @server.tool(
        name="acquire_documents",
        description=(
            "Обеспечивает наличие муниципального документа (ПЗЗ/генплан/…)"
            " в локальном хранилище geodocs: cache-first, затем статические"
            " порталы (файлы карточек РГИС, полный текст cntd), затем"
            " агентный ярус (LLM-поиск, МЕДЛЕННО — минуты). Возвращает status"
            " (cached/acquired/not_found/failed), refs с version_id"
            " для query_documents и warnings."
        ),
    )
    async def acquire_documents(
        municipality: str,
        doc_type: str,
        number: str | None = None,
        version_date: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                _acquire_documents,
                municipality,
                doc_type,
                number=number,
                version_date=version_date,
                title=title,
                home=home,
            )
        except Exception as exc:  # noqa: BLE001 - граница инструмента
            return {
                "status": "failed",
                "refs": [],
                "warnings": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        return result.model_dump(mode="json")

    @server.tool(
        name="query_documents",
        description=(
            "Семантический запрос к документам хранилища по version_ids"
            " (выдаёт acquire_documents). Сначала детерминированный fast-path"
            " по готовым extractions (ВРИ по кодам зон, режимы ЗОУИТ),"
            " промах — LLM-агент (МЕДЛЕННО). response_schema — JSON Schema"
            " для data. Возвращает status, data, evidence с цитатами,"
            " executor (static:vri/static:zouit/имя агента) и warnings."
        ),
    )
    async def query_documents(
        version_ids: list[int],
        query: str,
        response_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            result = await asyncio.to_thread(
                _query_documents,
                version_ids,
                query,
                response_schema,
                home=home,
            )
        except Exception as exc:  # noqa: BLE001 - граница инструмента
            return {
                "status": "failed",
                "data": None,
                "evidence": [],
                "warnings": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        return result.model_dump(mode="json")

    return server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geodocs-mcp",
        description="Публичный MCP-сервер второго контура geodocs.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--path", default="/mcp", dest="streamable_http_path")
    parser.add_argument("--stateful-http", action="store_true")
    parser.add_argument("--sse-response", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Точка входа geodocs-mcp: stdio по умолчанию, streamable-http по флагу."""
    args = _build_parser().parse_args(argv)
    server = create_server(
        host=args.host,
        port=args.port,
        streamable_http_path=args.streamable_http_path,
        stateless_http=not args.stateful_http,
        json_response=not args.sse_response,
    )
    _server_class, mcp2 = _load_server_class()
    if args.transport == "stdio" or not mcp2:
        server.run(transport=args.transport)
        return
    server.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        streamable_http_path=args.streamable_http_path,
        stateless_http=not args.stateful_http,
        json_response=not args.sse_response,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
