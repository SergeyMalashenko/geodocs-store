"""Тесты публичного MCP-сервера geodocs-mcp: поверхность, сериализация, ошибки."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from geodocs import (
    DocRole,
    DocType,
    DocumentRef,
    DocumentStore,
    ExtractionKind,
    ExtractionOrigin,
    FileRecord,
    SourceName,
)
from geodocs.server import _build_parser, create_server

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"

_VRI_TABLE_PAYLOAD = {
    "zone_code": "Ж-1",
    "zone_name": "Зона жилой застройки",
    "source_file": "2026-04-09_регламент.txt",
    "items": [{"row": "1", "code": "2.1", "name": "ИЖС", "raw": "2.1 ИЖС"}],
    "counts": {},
}


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    store = DocumentStore(tmp_path / "home" / "geodocs.sqlite3")
    store.close()
    return tmp_path / "home"


@pytest.fixture()
def seeded(home: Path, tmp_path: Path) -> int:
    """Версия pzz downloaded с txt-файлом и валидной extraction ВРИ Ж-1."""
    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        version_id = store.register_ref(
            DocumentRef(
                municipality=MUNICIPALITY,
                doc_type=DocType.PZZ,
                number="944",
                version_date=AMENDMENT_DATE,
                role=DocRole.AMENDMENT,
                title="Правила землепользования и застройки",
                source=SourceName.RGIS,
                source_object_id="13881025700",
            )
        )
        text_file = tmp_path / "2026-04-09_регламент.txt"
        text_file.write_text("Зона Ж-1: жилая застройка", encoding="utf-8")
        store.record_agent_fetch(
            version_id,
            files=[
                FileRecord(
                    path=str(text_file),
                    size=text_file.stat().st_size,
                    sha256=hashlib.sha256(text_file.read_bytes()).hexdigest(),
                    title=text_file.name,
                )
            ],
            source_url="https://solreg.ru/docs/944",
            fetched_at="2026-10-05T00:00:00+00:00",
        )
        store.upsert_extraction(
            version_id,
            zone_code="Ж-1",
            kind=ExtractionKind.VRI_TABLE,
            origin=ExtractionOrigin.FETCHED_FILE,
            payload=dict(_VRI_TABLE_PAYLOAD),
            extractor="test",
        )
    finally:
        store.close()
    return version_id


async def _run_tool(server: Any, action: Any) -> Any:
    """Прогон действия по MCP in-memory транспорту (как в pyrgis-agents)."""
    lowlevel = getattr(server, "_lowlevel_server", None) or server._mcp_server
    async with (
        create_client_server_memory_streams() as (
            (client_read, client_write),
            (server_read, server_write),
        ),
        anyio.create_task_group() as task_group,
    ):
        task_group.start_soon(
            lowlevel.run,
            server_read,
            server_write,
            lowlevel.create_initialization_options(),
            True,
        )
        async with ClientSession(client_read, client_write) as session:
            await session.initialize()
            result = await action(session)
        task_group.cancel_scope.cancel()
    return result


async def call_tool(server: Any, name: str, arguments: dict[str, Any]) -> Any:
    result = await _run_tool(
        server, lambda session: session.call_tool(name, arguments)
    )
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)
    assert not is_error, result.content
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    return json.loads(result.content[0].text)


@pytest.mark.asyncio()
async def test_server_lists_exactly_two_tools(home: Path) -> None:
    server = create_server(home=home)
    tools = await _run_tool(server, lambda session: session.list_tools())
    assert {tool.name for tool in tools.tools} == {
        "acquire_documents",
        "query_documents",
    }


@pytest.mark.asyncio()
async def test_acquire_documents_tool_cached(home: Path, seeded: int) -> None:
    result = await call_tool(
        create_server(home=home),
        "acquire_documents",
        {
            "municipality": MUNICIPALITY,
            "doc_type": "pzz",
            "number": "944",
            "version_date": AMENDMENT_DATE,
        },
    )
    assert result["status"] == "cached"
    (ref,) = result["refs"]
    assert ref["version_id"] == seeded
    assert ref["number"] == "944"
    json.dumps(result)  # сериализация не падает


@pytest.mark.asyncio()
async def test_acquire_documents_tool_invalid_doc_type(home: Path) -> None:
    result = await call_tool(
        create_server(home=home),
        "acquire_documents",
        {"municipality": MUNICIPALITY, "doc_type": "bogus", "number": "1"},
    )
    assert result["status"] == "failed"
    assert "bogus" in result["error"]


@pytest.mark.asyncio()
async def test_query_documents_tool_static_vri(home: Path, seeded: int) -> None:
    result = await call_tool(
        create_server(home=home),
        "query_documents",
        {"version_ids": [seeded], "query": "Верни ВРИ зоны Ж-1"},
    )
    assert result["status"] == "success"
    assert result["executor"] == "static:vri"
    (zone,) = result["data"]["zones"]
    assert zone["zone_code"] == "Ж-1"
    assert zone["found"] is True
    json.dumps(result)


@pytest.mark.asyncio()
async def test_query_documents_tool_unresolved(home: Path) -> None:
    result = await call_tool(
        create_server(home=home),
        "query_documents",
        {"version_ids": [999_999], "query": "Верни ВРИ зоны Ж-1"},
    )
    assert result["status"] == "not_found"
    assert result["warnings"]


def test_cli_parser_defaults() -> None:
    args = _build_parser().parse_args([])
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 8006
    assert args.streamable_http_path == "/mcp"


def test_cli_parser_http_options() -> None:
    args = _build_parser().parse_args(
        ["--transport", "streamable-http", "--port", "9000", "--path", "/mcp2"]
    )
    assert args.transport == "streamable-http"
    assert args.port == 9000
    assert args.streamable_http_path == "/mcp2"


def test_create_server_validates_port_and_path(home: Path) -> None:
    with pytest.raises(ValueError, match="port"):
        create_server(home=home, port=0)
    with pytest.raises(ValueError, match="streamable_http_path"):
        create_server(home=home, streamable_http_path="mcp")
