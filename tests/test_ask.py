"""Тесты Q&A поверх локальной базы: ask_document и read-only MCP-инструменты.

LLM-агент не запускается: для ask_document используется исполнитель
тестового типа "stub" (реестр, как в test_executors), read-only инструменты
(find_documents/document_files/read_document_text/get_extractions)
вызываются напрямую, без транспорта MCP.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

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
from geodocs.agent import (
    AgentTierConfig,
    AskResult,
    ExecutionResult,
    ExecutorConfig,
    ask_document,
    build_ask_prompt,
)
from geodocs.agent.mcp import (
    McpContext,
    document_files_impl,
    find_documents_impl,
    get_extractions_impl,
    read_document_text_impl,
)

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"

_TXT_CONTENT = "Зона Ж-1: жилая застройка многоэтажная, процент застройки до 40"


# ---------------------------------------------------------------------------
# Фикстуры store (в стиле test_executors.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """Временный GEODOCS_HOME с БД хранилища."""
    store = DocumentStore(tmp_path / "home" / "geodocs.sqlite3")
    store.close()
    return tmp_path / "home"


@pytest.fixture()
def store(home: Path) -> Iterator[DocumentStore]:
    store = DocumentStore(home / "geodocs.sqlite3")
    yield store
    store.close()


def _ref(number: str, version_date: str, **overrides: Any) -> DocumentRef:
    data: dict[str, Any] = {
        "municipality": MUNICIPALITY,
        "doc_type": DocType.PZZ,
        "number": number,
        "version_date": version_date,
        "role": DocRole.AMENDMENT,
        "title": "Правила землепользования и застройки городского округа Солнечногорск",
        "issuer": "Совет депутатов городского округа Солнечногорск",
        "region_code": "50",
        "source": SourceName.RGIS,
        "source_object_id": "13881025700",
        "amendment_number": number,
    }
    data.update(overrides)
    return DocumentRef(**data)


@pytest.fixture()
def seeded(store: DocumentStore, tmp_path: Path) -> int:
    """Версия с реальным txt-файлом и extraction таблицы ВРИ зоны Ж-1."""
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    text_file = tmp_path / "2026-04-09_регламент.txt"
    text_file.write_text(_TXT_CONTENT, encoding="utf-8")
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
        payload={"items": []},
        extractor="test",
    )
    return version_id


# ---------------------------------------------------------------------------
# build_ask_prompt / ask_document (исполнитель — тестовый тип "stub")
# ---------------------------------------------------------------------------

_STUB_BEHAVIORS: dict[str, Callable[[str, Path], ExecutionResult]] = {}


class StubExecutor:
    """Исполнитель тестового типа "stub": поведение подконтрольно тесту."""

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.name = cfg.name
        self.provider = SourceName.MANUAL

    def run(self, prompt: str, workdir: Path) -> ExecutionResult:
        behavior = _STUB_BEHAVIORS.get(self.name)
        if behavior is None:
            raise AssertionError(f"нет поведения для stub-исполнителя {self.name!r}")
        return behavior(prompt, workdir)


@pytest.fixture()
def stub_type() -> Iterator[None]:
    """Регистрирует тип "stub" и восстанавливает реестр после теста."""
    from geodocs.agent import executors as executors_module

    snapshot = dict(executors_module._EXECUTOR_TYPES)
    if "stub" not in executors_module._EXECUTOR_TYPES:
        executors_module.register_executor_type("stub", StubExecutor)
    _STUB_BEHAVIORS.clear()
    yield
    executors_module._EXECUTOR_TYPES.clear()
    executors_module._EXECUTOR_TYPES.update(snapshot)
    _STUB_BEHAVIORS.clear()


def _ask_config() -> AgentTierConfig:
    return AgentTierConfig(
        chain=["hermes"],
        executors={
            "hermes": ExecutorConfig(
                name="hermes",
                type="stub",
                command="stub",
                args=[],
                timeout_seconds=60,
            )
        },
        retry_attempts=1,
        retry_pause_seconds=0,
    )


def test_build_ask_prompt_contains_query() -> None:
    prompt = build_ask_prompt("Какой процент застройки в зоне Ж-1 Солнечногорска?")
    assert "Какой процент застройки в зоне Ж-1 Солнечногорска?" in prompt
    assert "ВОПРОС:" in prompt


def test_ask_document_returns_answer(
    home: Path, stub_type: Iterator[None]
) -> None:
    seen_prompts: list[str] = []

    def answer_behavior(prompt: str, workdir: Path) -> ExecutionResult:
        seen_prompts.append(prompt)
        return ExecutionResult(
            stdout="Процент застройки зоны Ж-1 — до 40%\n",
            stderr="",
            returncode=0,
            duration_seconds=0.1,
        )

    _STUB_BEHAVIORS["hermes"] = answer_behavior

    result = ask_document(
        "Какой процент застройки в зоне Ж-1?", home=home, config=_ask_config()
    )

    assert isinstance(result, AskResult)
    assert result.answer == "Процент застройки зоны Ж-1 — до 40%"
    assert result.executor == "hermes"
    assert result.returncode == 0
    assert result.duration_seconds >= 0
    (prompt,) = seen_prompts
    assert "Какой процент застройки в зоне Ж-1?" in prompt


# ---------------------------------------------------------------------------
# Read-only MCP-инструменты поверх посеянного store
# ---------------------------------------------------------------------------


def _ctx(home: Path) -> McpContext:
    return McpContext(inbox=home / "inbox", home=home)


def test_find_documents_impl_finds_seeded(home: Path, seeded: int) -> None:
    found = find_documents_impl(_ctx(home), "944")
    (version,) = found["versions"]
    assert version["version_id"] == seeded
    assert version["number"] == "944"
    assert version["municipality"] == MUNICIPALITY


def test_document_files_impl_lists_one_file(home: Path, seeded: int) -> None:
    result = document_files_impl(_ctx(home), seeded)
    (entry,) = result["files"]
    assert entry["index"] == 0
    assert entry["path"].endswith("2026-04-09_регламент.txt")
    assert entry["title"] == "2026-04-09_регламент.txt"
    assert entry["size"] > 0


def test_read_document_text_impl_reads_txt(home: Path, seeded: int) -> None:
    result = read_document_text_impl(_ctx(home), seeded)
    assert "error" not in result
    assert result["text"] == _TXT_CONTENT
    assert result["total_chars"] == len(_TXT_CONTENT)
    assert result["truncated"] is False


def test_read_document_text_impl_index_out_of_range(home: Path, seeded: int) -> None:
    result = read_document_text_impl(_ctx(home), seeded, file_index=5)
    assert "error" in result


def test_get_extractions_impl_returns_payload(home: Path, seeded: int) -> None:
    result = get_extractions_impl(_ctx(home), seeded)
    (extraction,) = result["extractions"]
    assert extraction["zone_code"] == "Ж-1"
    assert extraction["kind"] == "vri_table"
    assert extraction["origin"] == "fetched_file"
    assert extraction["payload"] == {"items": []}
