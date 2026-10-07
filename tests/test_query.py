"""Тесты второго контура: acquire_documents / query_documents и их tools.

LLM-агент не запускается: исполнитель тестового типа "stub" (как в
test_ask.py); инструменты find_document/import_document/read_document и
search_document_text вызываются напрямую, HTTP мокается respx.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from geodocs import (
    AcquireStatus,
    DocRole,
    DocType,
    DocumentRef,
    DocumentStore,
    ExtractionKind,
    ExtractionOrigin,
    FetchStatus,
    FileRecord,
    QueryStatus,
    SourceName,
    acquire_documents,
    query_documents,
)
from geodocs.agent import (
    AgentTierConfig,
    ExecutionResult,
    ExecutorConfig,
)
from geodocs.agent.mcp import (
    McpContext,
    find_document_impl,
    import_document_impl,
    read_document_impl,
    search_document_text_impl,
)
from geodocs.agent.portals import Candidate, DocQuery
from geodocs.agent.query import (
    RESULT_MARKER,
    ResolvedDocument,
    _resolve_documents,
    build_query_prompt,
    parse_result,
)

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"

_TXT_CONTENT = (
    "Зона Ж-1: жилая застройка многоэтажная, процент застройки до 40"
)
_BIG_PDF = b"%PDF-1.4\n" + b"A" * 31_000


# ---------------------------------------------------------------------------
# Фикстуры store и stub-исполнитель (паттерн test_ask.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def home(tmp_path: Path) -> Path:
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
        "title": "Правила землепользования и застройки",
        "issuer": "Совет депутатов",
        "region_code": "50",
        "source": SourceName.RGIS,
        "source_object_id": "13881025700",
        "amendment_number": number,
    }
    data.update(overrides)
    return DocumentRef(**data)


@pytest.fixture()
def seeded(store: DocumentStore, tmp_path: Path) -> int:
    """Версия downloaded с txt-файлом (2 «страницы») и extraction зоны Ж-1."""
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    text_file = tmp_path / "2026-04-09_регламент.txt"
    text_file.write_text(
        f"{_TXT_CONTENT}\fОхранная зона ЛЭП: ограничения строительства",
        encoding="utf-8",
    )
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
        payload={"items": [{"code": "2.1", "name": "ИЖС"}]},
        extractor="test",
    )
    return version_id


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
    from geodocs.agent import executors as executors_module

    snapshot = dict(executors_module._EXECUTOR_TYPES)
    if "stub" not in executors_module._EXECUTOR_TYPES:
        executors_module.register_executor_type("stub", StubExecutor)
    _STUB_BEHAVIORS.clear()
    yield
    executors_module._EXECUTOR_TYPES.clear()
    executors_module._EXECUTOR_TYPES.update(snapshot)
    _STUB_BEHAVIORS.clear()


def _stub_config() -> AgentTierConfig:
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


def _ctx(home: Path) -> McpContext:
    return McpContext(inbox=home / "inbox", home=home)


def _result_line(payload: dict[str, Any]) -> str:
    import json

    return f"{RESULT_MARKER} {json.dumps(payload, ensure_ascii=False)}"


def _exec_result(stdout: str, returncode: int = 0) -> ExecutionResult:
    return ExecutionResult(
        stdout=stdout, stderr="", returncode=returncode, duration_seconds=0.1
    )


# ---------------------------------------------------------------------------
# parse_result / build_query_prompt / _resolve_documents
# ---------------------------------------------------------------------------


def test_parse_result_extracts_json() -> None:
    stdout = f"промежуточный текст\n{_result_line({'status': 'not_found'})}\n"
    assert parse_result(stdout) == {"status": "not_found"}
    assert parse_result("нет маркера") is None
    assert parse_result(f"{RESULT_MARKER} {{битый") is None


def test_build_query_prompt_cards_and_schema() -> None:
    docs = [
        ResolvedDocument(
            version_id=13,
            municipality="Городской округ Коломна",
            doc_type="pzz",
            number="1198",
            version_date="2026-04-17",
            title="ПЗЗ Коломны",
        )
    ]
    prompt = build_query_prompt(docs, "Верни зоны ВРИ")
    assert "version_id=13" in prompt
    assert "Верни зоны ВРИ" in prompt
    assert RESULT_MARKER in prompt
    with_schema = build_query_prompt(
        docs, "Верни зоны ВРИ", {"type": "object", "required": ["zones"]}
    )
    assert '"zones"' in with_schema
    assert "валидный по схеме" in with_schema


def test_resolve_documents_int_and_ref(store: DocumentStore, seeded: int) -> None:
    resolved, warnings = _resolve_documents(
        store,
        [
            seeded,
            _ref("944", AMENDMENT_DATE, version_id=seeded),
            _ref("944", AMENDMENT_DATE),
            _ref("000", "2026-01-01"),
            999_999,
        ],
    )
    assert [doc.version_id for doc in resolved] == [seeded]
    assert len(warnings) == 2  # несуществующие ref и version_id


# ---------------------------------------------------------------------------
# query_documents (stub-исполнитель)
# ---------------------------------------------------------------------------


def test_query_documents_success(home: Path, seeded: int, stub_type: None) -> None:
    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        return _exec_result(
            "прочитал документ\n"
            + _result_line(
                {
                    "status": "success",
                    "data": {"territorial_zone": "Ж-1"},
                    "evidence": [
                        {
                            "version_id": seeded,
                            "file": "2026-04-09_регламент.txt",
                            "page": 1,
                            "quote": "Зона Ж-1: жилая застройка",
                        }
                    ],
                    "answer_text": "Зона Ж-1",
                }
            )
        )

    _STUB_BEHAVIORS["hermes"] = behavior
    result = query_documents(
        [seeded], "Верни территориальную зону", home=home, config=_stub_config()
    )
    assert result.status is QueryStatus.SUCCESS
    assert result.data == {"territorial_zone": "Ж-1"}
    assert result.evidence[0].page == 1
    assert result.evidence[0].quote == "Зона Ж-1: жилая застройка"
    assert result.answer_text == "Зона Ж-1"
    assert [doc.version_id for doc in result.documents] == [seeded]
    assert result.executor == "hermes"


def test_query_documents_evidence_downgrade(
    home: Path, seeded: int, stub_type: None
) -> None:
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: _exec_result(
        _result_line({"status": "success", "data": {"zone": "Ж-1"}, "evidence": []})
    )
    result = query_documents(
        [seeded], "зона", home=home, config=_stub_config(), evidence_required=True
    )
    assert result.status is QueryStatus.INSUFFICIENT_EVIDENCE
    assert any("evidence_required" in warning for warning in result.warnings)


def test_query_documents_retry_on_missing_result(
    home: Path, seeded: int, stub_type: None
) -> None:
    calls: list[str] = []

    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        calls.append(prompt)
        if len(calls) == 1:
            return _exec_result("ответил прозой без маркера")
        return _exec_result(
            _result_line(
                {
                    "status": "not_found",
                    "data": None,
                    "evidence": [],
                    "answer_text": "нет сведений",
                }
            )
        )

    _STUB_BEHAVIORS["hermes"] = behavior
    result = query_documents([seeded], "зона", home=home, config=_stub_config())
    assert result.status is QueryStatus.NOT_FOUND
    assert len(calls) == 2
    assert "ОТКЛОНЁН" in calls[1]


def test_query_documents_schema_retry_then_failed(
    home: Path, seeded: int, stub_type: None
) -> None:
    schema = {"type": "object", "required": ["zones"]}
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: _exec_result(
        _result_line(
            {
                "status": "success",
                "data": {"wrong": True},
                "evidence": [{"quote": "q"}],
            }
        )
    )
    result = query_documents(
        [seeded], "зоны", response_schema=schema, home=home, config=_stub_config()
    )
    assert result.status is QueryStatus.FAILED
    assert any("zones" in warning for warning in result.warnings)


def test_query_documents_unknown_status_failed(
    home: Path, seeded: int, stub_type: None
) -> None:
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: _exec_result(
        _result_line({"status": "maybe", "data": None})
    )
    result = query_documents([seeded], "зона", home=home, config=_stub_config())
    assert result.status is QueryStatus.FAILED
    assert any("maybe" in warning for warning in result.warnings)


def test_query_documents_unresolved_not_found(home: Path, stub_type: None) -> None:
    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        raise AssertionError("исполнитель не должен запускаться")

    _STUB_BEHAVIORS["hermes"] = behavior
    result = query_documents([999_999], "зона", home=home, config=_stub_config())
    assert result.status is QueryStatus.NOT_FOUND
    assert result.warnings


def test_query_documents_ref_by_requisites(
    home: Path, seeded: int, stub_type: None
) -> None:
    seen: list[str] = []

    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        seen.append(prompt)
        return _exec_result(_result_line({"status": "not_found", "data": None}))

    _STUB_BEHAVIORS["hermes"] = behavior
    result = query_documents(
        [_ref("944", AMENDMENT_DATE)], "зона", home=home, config=_stub_config()
    )
    assert result.status is QueryStatus.NOT_FOUND
    assert [doc.version_id for doc in result.documents] == [seeded]
    assert f"version_id={seeded}" in seen[0]


# ---------------------------------------------------------------------------
# search_document_text / read_document
# ---------------------------------------------------------------------------


def test_search_document_text_pages(home: Path, seeded: int) -> None:
    result = search_document_text_impl(_ctx(home), seeded, "многоэтажная")
    assert result["files_searched"] == 1
    (hit,) = result["hits"]
    assert hit["page"] == 1
    assert "жилая застройка многоэтажная" in hit["snippet"]
    # вторая «страница» (после \f)
    result = search_document_text_impl(_ctx(home), seeded, "ЛЭП")
    (hit,) = result["hits"]
    assert hit["page"] == 2
    # несколько совпадений на одной странице — несколько hit'ов
    result = search_document_text_impl(_ctx(home), seeded, "застройк")
    assert len(result["hits"]) == 2
    assert all(hit["page"] == 1 for hit in result["hits"])
    # нет совпадений
    assert search_document_text_impl(_ctx(home), seeded, "несуществующее")[
        "hits"
    ] == []


def test_read_document_card_without_query(home: Path, seeded: int) -> None:
    result = read_document_impl(_ctx(home), seeded)
    assert result["version"]["number"] == "944"
    assert result["version"]["fetch_status"] == "downloaded"
    assert len(result["files"]) == 1
    assert result["extractions"][0]["zone_code"] == "Ж-1"
    assert result["text_preview"]["text"].startswith("Зона Ж-1")


def test_read_document_query_zone_and_fragments(home: Path, seeded: int) -> None:
    result = read_document_impl(_ctx(home), seeded, query="ВРИ зоны Ж-1 застройка")
    (extraction,) = result["extractions"]
    assert extraction["zone_code"] == "Ж-1"
    assert extraction["payload"] == {"items": [{"code": "2.1", "name": "ИЖС"}]}
    assert result["fragments"], "по ключевым словам должны найтись фрагменты"
    assert any("застройки" in hit["snippet"] for hit in result["fragments"])


def test_read_document_unknown_version(home: Path) -> None:
    assert "error" in read_document_impl(_ctx(home), 4242)


# ---------------------------------------------------------------------------
# find_document
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_find_document_local_hit(home: Path, seeded: int) -> None:
    result = await find_document_impl(
        _ctx(home), MUNICIPALITY, "pzz", "944", AMENDMENT_DATE
    )
    assert result["local"] is True
    (version,) = result["versions"]
    assert version["version_id"] == seeded
    assert version["fetch_status"] == "downloaded"
    assert "candidates" not in result


@pytest.mark.asyncio()
async def test_find_document_external_on_not_downloaded(
    home: Path, store: DocumentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    store.set_fetch_status(version_id, FetchStatus.NOT_FOUND)

    class _FakePortal:
        name = "fake"

        async def search(self, query: DocQuery) -> list[Candidate]:
            return [
                Candidate(url="https://fake.ru/doc.pdf", title="ПЗЗ 944", portal="fake")
            ]

    import geodocs.agent.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "list_portals", lambda: ["fake"])
    monkeypatch.setattr(mcp_module, "get_portal", lambda name: _FakePortal())

    result = await find_document_impl(
        _ctx(home), MUNICIPALITY, "pzz", "944", AMENDMENT_DATE
    )
    assert result["local"] is False
    assert result["versions"][0]["fetch_status"] == "not_found"
    assert result["candidates"][0]["url"] == "https://fake.ru/doc.pdf"


@pytest.mark.asyncio()
async def test_find_document_note_without_number(home: Path) -> None:
    result = await find_document_impl(_ctx(home), "Несуществующий округ")
    assert result["local"] is False
    assert result["versions"] == []
    assert "note" in result


# ---------------------------------------------------------------------------
# import_document
# ---------------------------------------------------------------------------


_HINT = {
    "municipality": "Городской округ Тестовый",
    "doc_type": "pzz",
    "number": "77",
    "version_date": "2026-01-15",
    "title": "ПЗЗ Тестового",
}


@pytest.mark.asyncio()
async def test_import_document_direct_pdf(home: Path) -> None:
    ctx = _ctx(home)
    with respx.mock:
        respx.get("https://test-adm.ru/docs/pzz_77.pdf").mock(
            return_value=httpx.Response(
                200, content=_BIG_PDF, headers={"content-type": "application/pdf"}
            )
        )
        result = await import_document_impl(
            ctx, "https://test-adm.ru/docs/pzz_77.pdf", doc_hint=dict(_HINT)
        )
    assert "error" not in result
    assert result["fetch_status"] == "downloaded"
    (entry,) = result["files"]
    assert Path(entry["path"]).is_file()

    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        version = store.find_version(
            municipality=_HINT["municipality"],
            doc_type=DocType.PZZ,
            number="77",
            version_date="2026-01-15",
        )
        assert version is not None
        assert version.id == result["version_id"]
        assert version.fetch_status is FetchStatus.DOWNLOADED
        assert len(store.files_for_version(version.id)) == 1
    finally:
        store.close()


@pytest.mark.asyncio()
async def test_import_document_gate_rejects_small(home: Path) -> None:
    ctx = _ctx(home)
    small_pdf = b"%PDF-1.4\n" + b"A" * 100
    with respx.mock:
        respx.get("https://test-adm.ru/docs/small.pdf").mock(
            return_value=httpx.Response(
                200, content=small_pdf, headers={"content-type": "application/pdf"}
            )
        )
        result = await import_document_impl(
            ctx, "https://test-adm.ru/docs/small.pdf", doc_hint=dict(_HINT)
        )
    assert result["error"].startswith("gate_rejected")


@pytest.mark.asyncio()
async def test_import_document_html_page(home: Path) -> None:
    ctx = _ctx(home)
    page = """
    <html><body>
    <p>ПЗЗ, изменения № 77 от 15.01.2026</p>
    <a href="/files/pzz_77.pdf">Текст (PDF)</a>
    </body></html>
    """
    with respx.mock:
        respx.get("https://test-adm.ru/pzz").mock(
            return_value=httpx.Response(
                200, text=page, headers={"content-type": "text/html"}
            )
        )
        respx.get("https://test-adm.ru/files/pzz_77.pdf").mock(
            return_value=httpx.Response(
                200, content=_BIG_PDF, headers={"content-type": "application/pdf"}
            )
        )
        result = await import_document_impl(
            ctx, "https://test-adm.ru/pzz", doc_hint=dict(_HINT)
        )
    assert "error" not in result, result.get("error")
    assert result["fetch_status"] == "downloaded"
    assert result["files"][0]["title"].endswith(".pdf")


@pytest.mark.asyncio()
async def test_import_document_requires_hint(home: Path) -> None:
    ctx = _ctx(home)
    with respx.mock:
        respx.get("https://test-adm.ru/docs/pzz_77.pdf").mock(
            return_value=httpx.Response(
                200, content=_BIG_PDF, headers={"content-type": "application/pdf"}
            )
        )
        result = await import_document_impl(ctx, "https://test-adm.ru/docs/pzz_77.pdf")
    assert "doc_hint" in result["error"]
    assert result["inbox_files"]


# ---------------------------------------------------------------------------
# acquire_documents
# ---------------------------------------------------------------------------


def test_acquire_cached(home: Path, seeded: int) -> None:
    result = acquire_documents(
        MUNICIPALITY, "pzz", number="944", version_date=AMENDMENT_DATE, home=home
    )
    assert result.status is AcquireStatus.CACHED
    assert [ref.version_id for ref in result.refs] == [seeded]


def test_acquire_not_found_without_agent(home: Path) -> None:
    result = acquire_documents(
        MUNICIPALITY, "pzz", number="000", home=home, allow_agent=False
    )
    assert result.status is AcquireStatus.NOT_FOUND
    assert any("allow_agent" in warning for warning in result.warnings)


def test_acquire_needs_number_for_agent(home: Path) -> None:
    result = acquire_documents(MUNICIPALITY, "pzz", home=home)
    assert result.status is AcquireStatus.NOT_FOUND
    assert any("номер" in warning for warning in result.warnings)


def test_acquire_agent_downloads(
    home: Path, stub_type: None
) -> None:
    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        match = re.search(r"каталог (\S+)", prompt)
        assert match, "в промпте должен быть каталог inbox"
        inbox = Path(match.group(1))
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "2026-01-15_pzz_77.pdf").write_bytes(_BIG_PDF)
        return _exec_result(
            '=== MANIFEST === {"status": "found",'
            ' "files": ["2026-01-15_pzz_77.pdf"],'
            ' "source_url": "https://test-adm.ru/docs/pzz_77.pdf",'
            ' "notes": "ok", "steps_used": 2}'
        )

    _STUB_BEHAVIORS["hermes"] = behavior
    result = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="77",
        version_date="2026-01-15",
        title="ПЗЗ Тестового",
        home=home,
        config=_stub_config(),
    )
    assert result.status is AcquireStatus.ACQUIRED
    (ref,) = result.refs
    assert ref.version_id is not None
    assert ref.number == "77"

    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        version = store.find_version(
            municipality="Городской округ Тестовый",
            doc_type=DocType.PZZ,
            number="77",
            version_date="2026-01-15",
        )
        assert version is not None
        assert version.fetch_status is FetchStatus.DOWNLOADED
    finally:
        store.close()


def _version_status(home: Path, number: str, version_date: str) -> FetchStatus:
    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        version = store.find_version(
            municipality="Городской округ Тестовый",
            doc_type=DocType.PZZ,
            number=number,
            version_date=version_date,
        )
        assert version is not None
        return version.fetch_status
    finally:
        store.close()


def test_acquire_agent_not_found_marks_version(
    home: Path, stub_type: None
) -> None:
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: _exec_result(
        '=== MANIFEST === {"status": "not_found", "files": [],'
        ' "source_url": null, "notes": "нет такого", "steps_used": 3}'
    )
    result = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="88",
        version_date="2026-02-01",
        home=home,
        config=_stub_config(),
    )
    assert result.status is AcquireStatus.NOT_FOUND
    # попытка состоялась: версия не остаётся pending
    assert _version_status(home, "88", "2026-02-01") is FetchStatus.NOT_FOUND


def test_acquire_agent_error_marks_search_failed(
    home: Path, stub_type: None
) -> None:
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: ExecutionResult(
        stdout="", stderr="boom", returncode=1, duration_seconds=0.1
    )
    result = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="89",
        version_date="2026-02-01",
        home=home,
        config=_stub_config(),
    )
    assert result.status is AcquireStatus.FAILED
    assert _version_status(home, "89", "2026-02-01") is FetchStatus.SEARCH_FAILED


def test_acquire_without_executor_marks_search_failed(
    home: Path, stub_type: None
) -> None:
    empty_chain = AgentTierConfig(
        chain=["missing"], executors={}, retry_attempts=1, retry_pause_seconds=0
    )
    result = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="90",
        version_date="2026-02-01",
        home=home,
        config=empty_chain,
    )
    assert result.status is AcquireStatus.FAILED
    assert _version_status(home, "90", "2026-02-01") is FetchStatus.SEARCH_FAILED


# ---------------------------------------------------------------------------
# query_log: аудит запросов query_documents
# ---------------------------------------------------------------------------


def test_query_documents_persists_query_log(
    home: Path, seeded: int, stub_type: None
) -> None:
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: _exec_result(
        _result_line(
            {
                "status": "success",
                "data": {"territorial_zone": "Ж-1"},
                "evidence": [{"version_id": seeded, "quote": "Зона Ж-1"}],
                "answer_text": "Ж-1",
            }
        )
    )
    result = query_documents(
        [seeded], "Верни территориальную зону", home=home, config=_stub_config()
    )
    assert result.status is QueryStatus.SUCCESS

    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        (entry,) = store.query_log_entries()
        assert entry.query == "Верни территориальную зону"
        assert entry.status == "success"
        assert entry.version_ids == [seeded]
        assert entry.data == {"territorial_zone": "Ж-1"}
        assert entry.evidence[0]["quote"] == "Зона Ж-1"
        assert entry.answer_text == "Ж-1"
        assert entry.executor == "hermes"
    finally:
        store.close()


def test_query_documents_log_records_negative_outcomes(
    home: Path, seeded: int, stub_type: None
) -> None:
    # запрос без резолвящихся документов — исполнитель не вызывается
    result = query_documents([999_999], "зона", home=home, config=_stub_config())
    assert result.status is QueryStatus.NOT_FOUND

    # падение протокола: агент не вернул RESULT ни в попытке, ни в retry
    _STUB_BEHAVIORS["hermes"] = lambda prompt, workdir: _exec_result(
        "ответил прозой без маркера"
    )
    failed = query_documents([seeded], "зона", home=home, config=_stub_config())
    assert failed.status is QueryStatus.FAILED

    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        entries = store.query_log_entries()
        assert [entry.status for entry in entries] == ["failed", "not_found"]
        assert entries[0].version_ids == [seeded]
        assert entries[1].version_ids == []  # unresolved ref
        assert entries[0].executor == "hermes"
    finally:
        store.close()
