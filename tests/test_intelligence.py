"""Тесты инвариантов Document Intelligence (§52–53, §58, §83 ТЗ).

LLM не запускается: исполнитель тестового типа "stub" со счётчиком вызовов;
HTTP мокается respx. Фикстуры и стаб-инфраструктура — из test_query.py.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import httpx
import pytest
import respx

from geodocs import (
    AcquireStatus,
    DocumentStore,
    FetchStatus,
    FileRecord,
    QueryStatus,
    acquire_documents,
    query_documents,
)
from geodocs.agent import AgentTierConfig, ExecutionResult, ExecutorConfig
from geodocs.agent.match import match_document
from geodocs.agent.mcp import import_document_impl

from .test_query import (
    _BIG_PDF,
    _STUB_BEHAVIORS,
    _ctx,
    _exec_result,
    _ref,
    _result_line,
    _stub_config,
    home,
    seeded,
    store,
    stub_type,
)

__all__ = ["home", "seeded", "store", "stub_type"]  # re-export фикстур для pytest


def _calls(counter: dict[str, int], name: str = "hermes") -> int:
    return counter.get(name, 0)


def _counting(
    counter: dict[str, int], inner, name: str = "hermes"
):
    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        counter[name] = counter.get(name, 0) + 1
        return inner(prompt, workdir)

    return behavior


# ---------------------------------------------------------------------------
# §83: cache hit → Hermes не запускается; cache miss → запускается
# ---------------------------------------------------------------------------


def test_cache_hit_does_not_start_agent(
    home: Path, seeded: int, stub_type: None
) -> None:
    counter: dict[str, int] = {}
    _STUB_BEHAVIORS["hermes"] = _counting(
        counter, lambda p, w: (_ for _ in ()).throw(AssertionError("не должен звонить"))
    )
    result = acquire_documents(
        "Городской округ Солнечногорск",
        "pzz",
        number="944",
        version_date="2026-04-09",
        home=home,
        config=_stub_config(),
    )
    assert result.status is AcquireStatus.CACHED
    assert _calls(counter) == 0


def test_cache_miss_starts_agent(home: Path, stub_type: None) -> None:
    counter: dict[str, int] = {}

    def download(prompt: str, workdir: Path) -> ExecutionResult:
        import re

        match = re.search(r"каталог (\S+)", prompt)
        inbox = Path(match.group(1))
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "2026-01-15_pzz_77.pdf").write_bytes(_BIG_PDF)
        return _exec_result(
            '=== MANIFEST === {"status": "found",'
            ' "files": ["2026-01-15_pzz_77.pdf"],'
            ' "source_url": "https://test-adm.ru/docs/pzz_77.pdf",'
            ' "notes": "ok", "steps_used": 2}'
        )

    _STUB_BEHAVIORS["hermes"] = _counting(counter, download)
    result = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="77",
        version_date="2026-01-15",
        home=home,
        config=_stub_config(),
    )
    assert result.status is AcquireStatus.ACQUIRED
    assert _calls(counter) == 1


# ---------------------------------------------------------------------------
# §83: DocumentCandidate не становится DocumentVersion без validation (§52)
# ---------------------------------------------------------------------------


def _docx_with_text(text: str) -> bytes:
    """Минимальный валидный DOCX (>10 КБ порога гейта) с заданным текстом."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f"<w:p>{text}</w:p>" + " " * 11_000)
    return buffer.getvalue()


@pytest.mark.asyncio()
async def test_mismatch_document_not_registered(home: Path) -> None:
    """Файл с чужим номером отклоняется: mismatch — не регистрируем."""
    ctx = _ctx(home)
    foreign = _docx_with_text("Постановление № 999 от 03.03.2024 о чём-то другом")
    hint = {
        "municipality": "Городской округ Тестовый",
        "doc_type": "pzz",
        "number": "77",
        "version_date": "2026-01-15",
        "title": "ПЗЗ Тестового",
    }
    with respx.mock:
        respx.get("https://test-adm.ru/docs/other.docx").mock(
            return_value=httpx.Response(
                200,
                content=foreign,
                headers={
                    "content-type": "application/vnd.openxmlformats-officedocument"
                    ".wordprocessingml.document"
                },
            )
        )
        result = await import_document_impl(
            ctx, "https://test-adm.ru/docs/other.docx", doc_hint=hint
        )
    assert result["error"].startswith("document_mismatch")
    assert result["match"]["status"] == "mismatch"

    store = DocumentStore(home / "geodocs.sqlite3")
    try:
        version = store.find_version(
            municipality=hint["municipality"],
            doc_type="pzz",
            number="77",
            version_date="2026-01-15",
        )
        # версия могла быть зарегистрирована ref'ом, но файл не принят
        if version is not None:
            assert version.fetch_status is not FetchStatus.DOWNLOADED
        observations = store.negative_observations(
            "Городской округ Тестовый|pzz|77|2026-01-15", since_days=7
        )
        assert any(item["result"] == "mismatch" for item in observations)
    finally:
        store.close()


@pytest.mark.asyncio()
async def test_exact_match_registered(home: Path) -> None:
    """Совпавшие номер и дата → exact, документ регистрируется."""
    ctx = _ctx(home)
    docx = _docx_with_text("Постановление № 77 от 15.01.2026 об утверждении ПЗЗ")
    hint = {
        "municipality": "Городской округ Тестовый",
        "doc_type": "pzz",
        "number": "77",
        "version_date": "2026-01-15",
    }
    with respx.mock:
        respx.get("https://test-adm.ru/docs/pzz77.docx").mock(
            return_value=httpx.Response(200, content=docx)
        )
        result = await import_document_impl(
            ctx, "https://test-adm.ru/docs/pzz77.docx", doc_hint=hint
        )
    assert result["fetch_status"] == "downloaded"
    assert result["match"]["status"] == "exact"


# ---------------------------------------------------------------------------
# §83: duplicate → корректный dedup (фикс UNIQUE sha256)
# ---------------------------------------------------------------------------


def test_record_agent_fetch_dedup_same_content(store: DocumentStore, tmp_path: Path) -> None:
    first = store.register_ref(_ref("944", "2026-04-09"))
    second = store.register_ref(_ref("945", "2026-04-10"))
    payload = tmp_path / "same.pdf"
    payload.write_bytes(_BIG_PDF)
    digest = hashlib.sha256(_BIG_PDF).hexdigest()

    def file_record() -> FileRecord:
        return FileRecord(
            path=str(payload), size=len(_BIG_PDF), sha256=digest, title="same.pdf"
        )

    store.record_agent_fetch(
        first, files=[file_record()], source_url="https://a.ru/1",
        fetched_at="2026-10-01T00:00:00+00:00",
    )
    # повтор той же версии тем же файлом — идемпотентно
    store.record_agent_fetch(
        first, files=[file_record()], source_url="https://a.ru/1",
        fetched_at="2026-10-01T00:00:00+00:00",
    )
    # тот же контент другой версией — без UNIQUE-ошибки
    store.record_agent_fetch(
        second, files=[file_record()], source_url="https://a.ru/2",
        fetched_at="2026-10-02T00:00:00+00:00",
    )
    assert store.find_version(
        municipality="Городской округ Солнечногорск",
        doc_type="pzz",
        number="945",
        version_date="2026-04-10",
    ).fetch_status is FetchStatus.DOWNLOADED
    # владелец хэша один — первая версия
    owners = store.connection.execute(
        "SELECT COUNT(*) AS n FROM version_files WHERE sha256 = ?", (digest,)
    ).fetchone()
    assert owners["n"] == 1


# ---------------------------------------------------------------------------
# §83: Qwen недоступна → кэшированные документы остаются доступны
# ---------------------------------------------------------------------------


def test_cached_document_works_with_dead_chain(
    home: Path, seeded: int, stub_type: None
) -> None:
    dead = AgentTierConfig(
        chain=["hermes"],
        executors={
            "hermes": ExecutorConfig(
                name="hermes", type="stub", command="stub", args=[],
                timeout_seconds=1,
            )
        },
        retry_attempts=1,
        retry_pause_seconds=0,
    )
    _STUB_BEHAVIORS["hermes"] = lambda p, w: ExecutionResult(
        stdout="", stderr="boom", returncode=1, duration_seconds=0.1
    )
    result = acquire_documents(
        "Городской округ Солнечногорск",
        "pzz",
        number="944",
        version_date="2026-04-09",
        home=home,
        config=dead,
    )
    assert result.status is AcquireStatus.CACHED
    assert [ref.version_id for ref in result.refs] == [seeded]


# ---------------------------------------------------------------------------
# §53: SearchObservation — negative cache на повторный поиск
# ---------------------------------------------------------------------------


def test_negative_cache_blocks_repeat_search(home: Path, stub_type: None) -> None:
    counter: dict[str, int] = {}
    _STUB_BEHAVIORS["hermes"] = _counting(
        counter,
        lambda p, w: _exec_result(
            '=== MANIFEST === {"status": "not_found", "files": [],'
            ' "source_url": null, "notes": "нет", "steps_used": 3}'
        ),
    )
    first = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="88",
        version_date="2026-02-01",
        home=home,
        config=_stub_config(),
    )
    assert first.status is AcquireStatus.NOT_FOUND
    assert _calls(counter) == 1

    second = acquire_documents(
        "Городской округ Тестовый",
        "pzz",
        number="88",
        version_date="2026-02-01",
        home=home,
        config=_stub_config(),
    )
    assert second.status is AcquireStatus.NOT_FOUND
    assert _calls(counter) == 1  # повторно агент не запускался
    assert any("negative cache" in warning for warning in second.warnings)


# ---------------------------------------------------------------------------
# §58: кэш structured extraction
# ---------------------------------------------------------------------------


def test_answer_cache_skips_executor(
    home: Path, seeded: int, stub_type: None
) -> None:
    counter: dict[str, int] = {}

    def answer(prompt: str, workdir: Path) -> ExecutionResult:
        return _exec_result(
            _result_line(
                {
                    "status": "success",
                    "data": {"min_area": 500},
                    "evidence": [
                        {"version_id": seeded, "file": "f.txt", "page": 1,
                         "quote": "не менее 500"}
                    ],
                    "answer_text": "500 м²",
                }
            )
        )

    _STUB_BEHAVIORS["hermes"] = _counting(counter, answer)
    first = query_documents(
        [seeded], "Какая минимальная площадь?", home=home, config=_stub_config()
    )
    assert first.status is QueryStatus.SUCCESS
    assert _calls(counter) == 1

    second = query_documents(
        [seeded], "Какая минимальная площадь?", home=home, config=_stub_config()
    )
    assert _calls(counter) == 1  # исполнитель не вызывался
    assert second.executor == "answer-cache"
    assert second.data == {"min_area": 500}
    assert any("кэш" in warning for warning in second.warnings)

    # изменённый запрос — промах кэша, исполнитель вызывается
    third = query_documents(
        [seeded], "Какая максимальная площадь?", home=home, config=_stub_config()
    )
    assert _calls(counter) == 2
    assert third.executor == "hermes"


def test_answer_cache_skips_unproven(home: Path, seeded: int, stub_type: None) -> None:
    """Ответы без evidence в кэш не попадают (extract, don't infer)."""
    counter: dict[str, int] = {}
    _STUB_BEHAVIORS["hermes"] = _counting(
        counter,
        lambda p, w: _exec_result(
            _result_line(
                {"status": "not_found", "data": None, "evidence": [],
                 "answer_text": None}
            )
        ),
    )
    for _ in range(2):
        result = query_documents(
            [seeded], "Есть ли высотные ограничения?", home=home,
            config=_stub_config(),
        )
        assert result.status is QueryStatus.NOT_FOUND
    assert _calls(counter) == 2  # оба раза исполнитель: кэша нет


# ---------------------------------------------------------------------------
# §52: DocumentMatch — статусы
# ---------------------------------------------------------------------------


def test_match_document_statuses() -> None:
    exact = match_document(
        number="77", version_date="2026-01-15",
        text="Постановление № 77 от 15.01.2026", source_url=None,
    )
    assert exact.status == "exact"
    assert exact.detected_number == "77"
    assert exact.detected_date == "2026-01-15"

    probable = match_document(
        number="77", version_date="2026-01-15",
        text="Правила землепользования № 77 (дата на скане не читается)",
        source_url=None,
    )
    assert probable.status == "probable"

    ambiguous = match_document(
        number="77", version_date="2026-01-15",
        text="Текст без реквизитов вообще", source_url=None,
    )
    assert ambiguous.status == "ambiguous"

    mismatch = match_document(
        number="77", version_date="2026-01-15",
        text="Постановление № 999 от 03.03.2024", source_url=None,
    )
    assert mismatch.status == "mismatch"

    # номер с лидирующими нулями и № vs N — совпадают
    assert match_document(
        number="0077", version_date=None,
        text="N 77 от 15.01.2026", source_url=None,
    ).detected_number == "77"


# ---------------------------------------------------------------------------
# Профили Hermes: discovery vs QA (два профиля, §43–45/§65–66 ТЗ)
# ---------------------------------------------------------------------------


def test_discovery_profile_env_override(monkeypatch, tmp_path: Path) -> None:
    from geodocs.agent import executors

    custom = tmp_path / "custom-discovery"
    custom.mkdir()
    monkeypatch.setenv("GEODOCS_HERMES_HOME", str(custom))
    assert executors._hermes_source_home() == custom


def test_discovery_profile_explicit_env_beats_discovery_dir(
    monkeypatch, tmp_path: Path
) -> None:
    from geodocs.agent import executors

    monkeypatch.delenv("GEODOCS_HERMES_HOME", raising=False)
    legacy = tmp_path / "legacy"
    monkeypatch.setenv("HERMES_HOME", str(legacy))
    discovery = tmp_path / ".hermes-discovery"
    discovery.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    # явный HERMES_HOME приоритетнее неявного дефолта ~/.hermes-discovery
    assert executors._hermes_source_home() == legacy


def test_discovery_profile_defaults_to_discovery_dir(
    monkeypatch, tmp_path: Path
) -> None:
    from geodocs.agent import executors

    monkeypatch.delenv("GEODOCS_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    discovery = tmp_path / ".hermes-discovery"
    discovery.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert executors._hermes_source_home() == discovery


def test_discovery_profile_legacy_fallback(monkeypatch, tmp_path: Path) -> None:
    from geodocs.agent import executors

    monkeypatch.delenv("GEODOCS_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)  # без .hermes-discovery
    assert executors._hermes_source_home() == tmp_path / ".hermes"
