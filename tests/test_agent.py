"""Тесты агентного яруса: задачи, промпт, гейт, запись в store, runner.

Без сети и без реального hermes: внешний агент подменяется shell-скриптом.
"""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from geodocs import (
    DocRole,
    DocType,
    DocumentRef,
    DocumentStore,
    FetchStatus,
    FileRecord,
    SourceName,
)
from geodocs.agent import (
    MANIFEST_MARKER,
    AgentTask,
    AgentTierConfig,
    Executor,
    ExecutorConfig,
    FileVerdict,
    build_executor,
    build_prompt,
    doc_type_ru,
    gate_pass,
    list_pending_tasks,
    parse_manifest,
    run_pending,
    run_task,
    verify_files,
)
from geodocs.agent.cli import main, recover_pending

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HermesExecutor читает профиль $HERMES_HOME: изолируем от реального ~/.hermes."""
    profile = tmp_path / "hermes-src"
    profile.mkdir()
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profile / "auth.json").write_text("{}", encoding="utf-8")
    (profile / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))


# ---------------------------------------------------------------------------
# Файлы-примитивы: настоящие сигнатуры, размеры с запасом над порогами гейта.
# ---------------------------------------------------------------------------


def _pdf_bytes(size: int = 33_000) -> bytes:
    """Минимальный одностраничный PDF ручной сборки, добитый до размера."""
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj\n"
        b"trailer << /Root 1 0 R >>\n"
        b"%" + repr(time.time_ns()).encode("ascii") + b"\n"
    )
    filler = b"%" + b"A" * max(0, size - len(body) - len(b"%%EOF\n"))
    return body + filler + b"%%EOF\n"


def _docx_bytes(size: int = 11_000) -> bytes:
    """DOCX-архив с word/document.xml, добитый до размера."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        content = "<w:document><w:p>текст</w:p></w:document>".encode()
        padding = b" " * max(0, size - len(content) - 512)
        archive.writestr("word/document.xml", content + padding)
    return buffer.getvalue()


def _png_bytes(size: int = 33_000) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _jpeg_bytes(size: int = 33_000) -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * (size - 4)


def _rar_bytes(size: int = 2_000) -> bytes:
    return b"Rar!\x1a\x07\x01\x00" + b"\x00" * (size - 8)


def _zip_bytes(size: int = 2_000) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data.txt", b"x" * size)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[DocumentStore]:
    # каталог БД имитирует GEODOCS_HOME: files/, inbox/, agent/ — рядом
    store = DocumentStore(tmp_path / "home" / "geodocs.sqlite3")
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
def seeded(store: DocumentStore) -> dict[str, int]:
    """Три версии: not_found, pending (умолчание), downloaded."""
    ids = {
        "not_found": store.register_ref(_ref("944", AMENDMENT_DATE)),
        "pending": store.register_ref(
            _ref("592", "2021-04-21", role=DocRole.BASE, amendment_number=None)
        ),
        "downloaded": store.register_ref(
            _ref("1756", "2022-01-01", role=DocRole.SINGLE)
        ),
    }
    store.set_fetch_status(ids["not_found"], FetchStatus.NOT_FOUND)
    store.set_fetch_status(ids["downloaded"], FetchStatus.DOWNLOADED)
    return ids


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _record(path: Path) -> FileRecord:
    return FileRecord(
        path=str(path),
        size=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        title=path.name,
    )


def _fake_agent(
    tmp_path: Path,
    inbox: Path,
    files: dict[str, bytes],
    manifest: dict[str, Any] | None,
    *,
    no_manifest_echo: str | None = None,
) -> Path:
    """Shell-заглушка агента: копирует файлы в inbox и печатает манифест.

    manifest=None имитирует агента, убитого до печати MANIFEST: вместо
    маркера — произвольная последняя строка `no_manifest_echo`.
    """
    staging = tmp_path / f"staging_{abs(hash(tuple(files))) % 10_000}_{time.time_ns() % 10_000}"
    staging.mkdir(exist_ok=False)
    names = []
    for name, data in files.items():
        (staging / name).write_bytes(data)
        names.append(name)
    if manifest is not None:
        manifest = {**manifest, "files": names}
        final_line = "echo " + shlex.quote(
            MANIFEST_MARKER + " " + json.dumps(manifest, ensure_ascii=False)
        )
    else:
        final_line = "echo " + shlex.quote(no_manifest_echo or "готово, файлы скачаны")
    copy_line = (
        f"cp {shlex.quote(str(staging))}/* {shlex.quote(str(inbox))}/"
        if files
        else ""
    )
    script = tmp_path / f"fake_agent_{staging.name}.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f"mkdir -p {shlex.quote(str(inbox))}\n"
        f"{copy_line}\n"
        f"{final_line}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


# Конфиг ровно на один проход: старые тесты раннера проверяют одиночный
# запуск без retry и failover.
_SINGLE_PASS = AgentTierConfig(
    chain=["hermes"], executors={}, retry_attempts=1, retry_pause_seconds=0
)


def _script_executor(script: Path, *, name: str = "hermes") -> Executor:
    """Исполнитель type=hermes поверх shell-заглушки (промт передаётся аргументом)."""
    return build_executor(
        ExecutorConfig(
            name=name,
            type="hermes",
            command=str(script),
            args=[],
            timeout_seconds=60,
            quota_patterns=[],
        )
    )


# ---------------------------------------------------------------------------
# list_pending_tasks / slug
# ---------------------------------------------------------------------------


def test_list_pending_tasks_filters_statuses(
    store: DocumentStore, seeded: dict[str, int]
) -> None:
    tasks = list_pending_tasks(store)
    assert [task.version_id for task in tasks] == [
        seeded["not_found"],
        seeded["pending"],
    ]

    only_pending = list_pending_tasks(store, statuses=("pending",))
    assert [task.version_id for task in only_pending] == [seeded["pending"]]

    task = tasks[0]
    assert task.doc_type == "pzz"
    assert task.number == "944"
    assert task.municipality == MUNICIPALITY
    assert task.version_date == AMENDMENT_DATE
    assert task.issuer is not None
    assert task.title is not None


def test_slug_replaces_separators(store: DocumentStore) -> None:
    version_id = store.register_ref(
        _ref(
            "50-11/0020310-49",
            "2026-01-15",
            doc_type=DocType.ZOUIT_REGIME,
            source_object_id="zouit-1",
        )
    )
    (task,) = [t for t in list_pending_tasks(store) if t.version_id == version_id]
    assert task.slug == "zouit_regime_50_11_0020310_49"


def test_list_pending_tasks_empty_statuses(store: DocumentStore) -> None:
    assert list_pending_tasks(store, statuses=()) == []


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


def _task(**overrides: Any) -> AgentTask:
    data: dict[str, Any] = {
        "version_id": 1,
        "doc_type": "pzz",
        "number": "944",
        "municipality": MUNICIPALITY,
        "title": "Правила землепользования и застройки",
        "issuer": "Совет депутатов",
        "version_date": AMENDMENT_DATE,
    }
    data.update(overrides)
    return AgentTask(**data)


def test_build_prompt_substitutes_fields() -> None:
    prompt = build_prompt(_task(), Path("/tmp/inbox/pzz_944"))
    assert "pzz_944" in prompt
    assert "Правила землепользования и застройки (изменения)" in prompt
    assert "№ 944 от 2026-04-09" in prompt
    assert MUNICIPALITY in prompt
    assert "Совет депутатов" in prompt
    assert "/tmp/inbox/pzz_944" in prompt
    assert MANIFEST_MARKER in prompt
    for placeholder in ("{slug}", "{doc_type_ru}", "{number}", "{date}", "{inbox_dir}"):
        assert placeholder not in prompt


@pytest.mark.parametrize(
    ("doc_type", "expected"),
    [
        ("pzz", "Правила землепользования и застройки (изменения)"),
        ("general_plan", "Генеральный план (изменения)"),
        ("zouit_regime", "Постановление о режимах использования земель (ЗОУИТ)"),
        ("custom_type", "custom_type"),
    ],
)
def test_doc_type_ru(doc_type: str, expected: str) -> None:
    assert doc_type_ru(doc_type) == expected


# ---------------------------------------------------------------------------
# parse_manifest
# ---------------------------------------------------------------------------


_GOOD_MANIFEST = {
    "status": "found",
    "files": ["2026-04-09_решение.pdf"],
    "source_url": "https://solreg.ru/docs/944",
    "page_title": "Решение 944",
    "notes": "",
    "steps_used": 3,
}


def test_parse_manifest_inline_json() -> None:
    stdout = "шаг 1\nшаг 2\n" + MANIFEST_MARKER + " " + json.dumps(
        _GOOD_MANIFEST, ensure_ascii=False
    )
    assert parse_manifest(stdout) == _GOOD_MANIFEST


def test_parse_manifest_multiline_json() -> None:
    stdout = "шаг 1\n" + MANIFEST_MARKER + "\n" + json.dumps(_GOOD_MANIFEST)
    assert parse_manifest(stdout) == _GOOD_MANIFEST


def test_parse_manifest_takes_last_marker() -> None:
    stdout = (
        MANIFEST_MARKER + ' {"status": "not_found"}\n'
        + MANIFEST_MARKER
        + " "
        + json.dumps(_GOOD_MANIFEST, ensure_ascii=False)
    )
    assert parse_manifest(stdout) == _GOOD_MANIFEST


def test_parse_manifest_no_marker() -> None:
    assert parse_manifest("ничего не найдено\n") is None


def test_parse_manifest_broken_json() -> None:
    assert parse_manifest(MANIFEST_MARKER + " {не json}\n") is None
    assert parse_manifest(MANIFEST_MARKER + "\n{тоже не json") is None


def test_parse_manifest_indented_marker() -> None:
    """Агенты печатают маркер с отступом (вложенность в маркдаун-список)."""
    stdout = (
        "рассуждение\n"
        '  === MANIFEST === {"status": "not_found", "files": [], "notes": "нет"}\n'
    )
    assert parse_manifest(stdout) == {"status": "not_found", "files": [], "notes": "нет"}


# ---------------------------------------------------------------------------
# verify_files / gate_pass
# ---------------------------------------------------------------------------


def test_verify_pdf_ok(tmp_path: Path) -> None:
    path = _write(tmp_path, "doc.pdf", _pdf_bytes())
    (verdict,) = verify_files([path])
    assert verdict.ok
    assert verdict.kind == "pdf"
    assert verdict.size == path.stat().st_size
    assert verdict.path == str(path)


def test_verify_small_pdf_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, "small.pdf", b"%PDF-1.4\n" + b"A" * 100)
    (verdict,) = verify_files([path])
    assert not verdict.ok
    assert verdict.kind == "pdf"
    assert "порога" in verdict.reason


def test_verify_docx_ok_and_plain_zip(tmp_path: Path) -> None:
    docx = _write(tmp_path, "doc.docx", _docx_bytes())
    plain_zip = _write(tmp_path, "plain.zip", _zip_bytes())
    verdicts = verify_files([docx, plain_zip])
    assert [v.kind for v in verdicts] == ["docx", "zip"]
    assert all(v.ok for v in verdicts)


def test_verify_legacy_doc_ok(tmp_path: Path) -> None:
    legacy = _write(
        tmp_path,
        "act.doc",
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 11_000,
    )
    verdict = verify_files([legacy])[0]
    assert verdict.kind == "doc"
    assert verdict.ok


def test_verify_images_and_rar(tmp_path: Path) -> None:
    files = [
        _write(tmp_path, "img.png", _png_bytes()),
        _write(tmp_path, "img.jpg", _jpeg_bytes()),
        _write(tmp_path, "pack.rar", _rar_bytes()),
    ]
    verdicts = verify_files(files)
    assert [v.kind for v in verdicts] == ["png", "jpeg", "rar"]
    assert all(v.ok for v in verdicts)


def test_verify_unknown_and_missing(tmp_path: Path) -> None:
    broken = _write(tmp_path, "broken.pdf", "совсем не pdf".encode())
    missing = tmp_path / "нет_файла.pdf"
    verdicts = verify_files([broken, missing])
    assert verdicts[0].kind == "unknown" and not verdicts[0].ok
    assert verdicts[1].kind == "missing" and not verdicts[1].ok


def _verdict(ok: bool) -> FileVerdict:
    return FileVerdict(
        path="f.pdf", size=100, kind="pdf", ok=ok, reason="ok" if ok else "плохо"
    )


def test_gate_pass_rules() -> None:
    """Гейт: ≥1 вердикт и все ok; пустой список, смешанный и плохой — False."""
    assert gate_pass([_verdict(True)])
    assert gate_pass([_verdict(True), _verdict(True)])
    assert not gate_pass([])
    assert not gate_pass([_verdict(False)])
    assert not gate_pass([_verdict(True), _verdict(False)])


# ---------------------------------------------------------------------------
# record_agent_fetch
# ---------------------------------------------------------------------------


def test_record_agent_fetch_primary_and_sections(
    store: DocumentStore, tmp_path: Path
) -> None:
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    plain = _write(tmp_path, "2026-04-09_приложение.pdf", _pdf_bytes())
    named = _write(tmp_path, "2026-04-09_изменения_944.pdf", _pdf_bytes())
    map_file = _write(tmp_path, "карта_Ж-2.png", _png_bytes())
    regime = _write(tmp_path, "регламент_зоны_Ж-2.pdf", _pdf_bytes())
    doc = _write(tmp_path, "2026-04-09_постановление.docx", _docx_bytes())
    files = [_record(p) for p in (plain, named, map_file, regime, doc)]

    fetched_at = "2026-10-05T00:00:00+00:00"
    store.record_agent_fetch(
        version_id,
        files=files,
        source_url="https://solreg.ru/docs/944",
        fetched_at=fetched_at,
    )

    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    # primary: первый PDF с «изменен» в имени, хотя обычный PDF шёл первым
    assert version.file_path == str(named)
    assert version.sha256 == hashlib.sha256(named.read_bytes()).hexdigest()
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.HERMES_AGENT
    assert version.source_url == "https://solreg.ru/docs/944"
    assert version.fetched_at is not None
    assert version.fetched_at.isoformat() == fetched_at

    stored = store.files_for_version(version_id)
    assert len(stored) == 5
    sections = {Path(f.file_path).name: f.section for f in stored}
    assert sections["2026-04-09_изменения_944.pdf"] == "текст"
    assert sections["2026-04-09_постановление.docx"] == "текст"
    assert sections["карта_Ж-2.png"] == "карта"
    assert sections["регламент_зоны_Ж-2.pdf"] == "регламент"
    assert sections["2026-04-09_приложение.pdf"] == "файл"

    refs = store.source_refs_for_version(version_id)
    assert ("hermes-agent", "https://solreg.ru/docs/944") in refs


def test_record_agent_fetch_idempotent(
    store: DocumentStore, tmp_path: Path
) -> None:
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    files = [_record(_write(tmp_path, "2026-04-09_решение.pdf", _pdf_bytes()))]
    kwargs = {
        "files": files,
        "source_url": "https://solreg.ru/docs/944",
        "fetched_at": "2026-10-05T00:00:00+00:00",
    }
    store.record_agent_fetch(version_id, **kwargs)
    store.record_agent_fetch(version_id, **kwargs)

    assert len(store.files_for_version(version_id)) == 1
    refs = store.source_refs_for_version(version_id)
    assert refs.count(("hermes-agent", "https://solreg.ru/docs/944")) == 1


def test_record_agent_fetch_primary_fallbacks(
    store: DocumentStore, tmp_path: Path
) -> None:
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    png = _write(tmp_path, "карта.png", _png_bytes())
    doc = _write(tmp_path, "методичка.docx", _docx_bytes())
    store.record_agent_fetch(
        version_id,
        files=[_record(png), _record(doc)],
        source_url="https://solreg.ru/docs/944",
        fetched_at="2026-10-05T00:00:00+00:00",
    )
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    # без PDF первичным становится первый файл
    assert version.file_path == str(png)


def test_record_agent_fetch_rejects_empty(store: DocumentStore) -> None:
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    with pytest.raises(ValueError):
        store.record_agent_fetch(
            version_id,
            files=[],
            source_url="https://solreg.ru/docs/944",
            fetched_at="2026-10-05T00:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# run_task с поддельным агентом
# ---------------------------------------------------------------------------


def test_run_task_downloaded(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    inbox = home / "inbox" / task.slug
    script = _fake_agent(
        tmp_path,
        inbox,
        {
            "2026-04-09_решение.pdf": _pdf_bytes(),
            "2026-04-09_карта.png": _png_bytes(),
        },
        {
            "status": "found",
            "source_url": "https://solreg.ru/docs/944",
            "page_title": "Решение 944",
            "notes": "",
            "steps_used": 4,
        },
    )

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "downloaded"
    assert result.error is None
    assert result.manifest is not None
    assert result.manifest["source_url"] == "https://solreg.ru/docs/944"
    assert len(result.verdicts) == 2
    assert all(v.ok for v in result.verdicts)
    assert result.duration_s >= 0

    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.HERMES_AGENT
    assert version.source_url == "https://solreg.ru/docs/944"

    stored = store.files_for_version(task.version_id)
    assert len(stored) == 2
    assert all(Path(f.file_path).exists() for f in stored)
    target_dir = (
        store.files_dir / MUNICIPALITY.replace(" ", "_") / "pzz_944"
    )
    assert {Path(f.file_path).parent for f in stored} == {target_dir}

    saved = json.loads(
        (home / "agent" / "results" / f"{task.slug}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] == "downloaded"
    assert saved["task"]["slug"] == task.slug
    assert saved["manifest"]["status"] == "found"


def test_run_task_not_found_keeps_db(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _fake_agent(
        tmp_path,
        home / "inbox" / task.slug,
        {},
        {"status": "not_found", "source_url": "", "notes": "только платный источник"},
    )

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "not_found"
    assert result.error is None
    assert result.manifest is not None
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND
    assert store.files_for_version(task.version_id) == []


def test_run_task_gate_failed_keeps_db(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _fake_agent(
        tmp_path,
        home / "inbox" / task.slug,
        {"2026-04-09_решение.pdf": "совсем не pdf".encode()},
        {"status": "found", "source_url": "https://solreg.ru/bad"},
    )

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "manual_required"  # финальная неудача эскалирует
    assert result.error
    assert not all(v.ok for v in result.verdicts)
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND
    assert store.files_for_version(task.version_id) == []


def test_run_task_agent_error_no_manifest(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = tmp_path / "silent_agent.sh"
    script.write_text("#!/bin/bash\necho 'работал, но ничего не нашел'\n", encoding="utf-8")
    script.chmod(0o755)

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "manual_required"  # финальная неудача эскалирует
    assert result.manifest is None
    assert result.error
    assert result.stdout_tail and "ничего не нашел" in result.stdout_tail
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND


def test_run_task_recovers_valid_inbox_without_manifest(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    """Агент скачал файлы, но умер до печати MANIFEST — дожим по уликам inbox."""
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _fake_agent(
        tmp_path,
        home / "inbox" / task.slug,
        {"2026-04-09_решение.pdf": _pdf_bytes()},
        None,  # манифест не печатается
        no_manifest_echo="квота исчерпана, падаю",
    )

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "downloaded"
    assert result.manifest is None
    assert result.error == "manifest missing; recovered from inbox evidence"
    assert result.stdout_tail and "квота исчерпана" in result.stdout_tail

    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.HERMES_AGENT
    assert version.source_url is None  # URL из манифеста неизвестен
    assert len(store.files_for_version(task.version_id)) == 1
    assert ("hermes-agent", "https://solreg.ru/docs/944") not in (
        store.source_refs_for_version(task.version_id)
    )

    saved = json.loads(
        (home / "agent" / "results" / f"{task.slug}.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["status"] == "downloaded"
    assert saved["stdout_tail"]


def test_run_task_broken_inbox_without_manifest_escalates(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    """Файлы из inbox не проходят гейт — финальный статус manual_required."""
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _fake_agent(
        tmp_path,
        home / "inbox" / task.slug,
        {"2026-04-09_решение.pdf": b"tiny"},
        None,
    )

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "manual_required"
    assert result.manifest is None
    assert result.error and "MANIFEST" in result.error
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND
    assert store.files_for_version(task.version_id) == []


def _sleeping_agent(tmp_path: Path, inbox: Path, files: dict[str, bytes]) -> Path:
    """Shell-заглушка медленного агента: копирует файлы и засыпает (таймаут)."""
    staging = tmp_path / f"staging_sleep_{time.time_ns() % 10_000}"
    staging.mkdir(exist_ok=False)
    for name, data in files.items():
        (staging / name).write_bytes(data)
    copy_line = (
        f"cp {shlex.quote(str(staging))}/* {shlex.quote(str(inbox))}/\n"
        if files
        else ""
    )
    script = tmp_path / f"sleeping_agent_{staging.name}.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f"mkdir -p {shlex.quote(str(inbox))}\n"
        f"{copy_line}"
        "sleep 30\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _sleeping_executor(script: Path) -> Executor:
    return build_executor(
        ExecutorConfig(
            name="hermes",
            type="hermes",
            command=str(script),
            args=[],
            timeout_seconds=2,
            quota_patterns=[],
        )
    )


def test_run_task_recovers_inbox_on_timeout(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    """Таймаут (медленная модель), но файлы скачаны: дожим по уликам inbox.

    page_text.txt рядом с PDF гейт не отравляет — recovery берёт только
    файлы-документы.
    """
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _sleeping_agent(
        tmp_path,
        home / "inbox" / task.slug,
        {
            "2026-04-09_решение.pdf": _pdf_bytes(),
            "page_text.txt": b"page text",  # не документ
        },
    )

    result = run_task(
        task,
        store,
        executors=[_sleeping_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "downloaded"
    assert result.error is not None
    assert "не уложился" in result.error
    assert "recovered from inbox evidence after timeout" in result.error
    stored = store.files_for_version(task.version_id)
    assert [Path(f.file_path).name for f in stored] == ["2026-04-09_решение.pdf"]
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED


def test_run_task_timeout_empty_inbox_escalates(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    """Таймаут без файлов в inbox — дожимать нечего: manual_required."""
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _sleeping_agent(tmp_path, home / "inbox" / task.slug, {})

    result = run_task(
        task,
        store,
        executors=[_sleeping_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "manual_required"
    assert result.error and "не уложился" in result.error
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND
    assert store.files_for_version(task.version_id) == []


def test_run_task_recovers_inbox_ignores_page_text(
    store: DocumentStore, seeded: dict[str, int], tmp_path: Path
) -> None:
    """Без манифеста: page_text.txt рядом с валидным PDF не ломает recovery."""
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    home = store.db_path.parent
    script = _fake_agent(
        tmp_path,
        home / "inbox" / task.slug,
        {
            "2026-04-09_решение.pdf": _pdf_bytes(),
            "page_text.txt": b"page text",
        },
        None,
    )

    result = run_task(
        task,
        store,
        executors=[_script_executor(script)],
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert result.status == "downloaded"
    stored = store.files_for_version(task.version_id)
    assert [Path(f.file_path).name for f in stored] == ["2026-04-09_решение.pdf"]


def test_run_pending_limit_and_pause(
    store: DocumentStore,
    seeded: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = store.db_path.parent
    first = list_pending_tasks(store)[0]
    script = _fake_agent(
        tmp_path,
        home / "inbox" / first.slug,
        {"2026-04-09_решение.pdf": _pdf_bytes()},
        {"status": "found", "source_url": "https://solreg.ru/docs/944"},
    )
    sleeps: list[float] = []
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", sleeps.append)

    results = run_pending(
        store,
        limit=1,
        executors={"hermes": _script_executor(script)},
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert len(results) == 1
    assert results[0].status == "downloaded"
    assert sleeps == []  # одна задача — пауза между задачами не нужна


def test_run_pending_polite_pause_between_tasks(
    store: DocumentStore,
    seeded: dict[str, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = store.db_path.parent
    script = _fake_agent(
        tmp_path,
        home / "inbox" / "unused",
        {},
        {"status": "not_found"},
    )
    sleeps: list[float] = []
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", sleeps.append)

    results = run_pending(
        store,
        executors={"hermes": _script_executor(script)},
        config=_SINGLE_PASS,
        geodocs_home=home,
    )

    assert len(results) == 2  # not_found + pending
    assert sleeps == [5.0]  # пауза ровно между двумя задачами


# ---------------------------------------------------------------------------
# recover: приём готовых файлов из inbox без вызова агента
# ---------------------------------------------------------------------------


def _seed_inbox(home: Path, slug: str, files: dict[str, bytes]) -> Path:
    inbox = home / "inbox" / slug
    inbox.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (inbox / name).write_bytes(data)
    return inbox


def test_recover_pending_recovers_and_skips(
    store: DocumentStore, seeded: dict[str, int]
) -> None:
    home = store.db_path.parent
    _seed_inbox(home, "pzz_944", {"2026-04-09_решение.pdf": _pdf_bytes()})
    # pzz_592: inbox пуст (каталога нет) — пропуск

    results = recover_pending(store, geodocs_home=home)

    assert [r.status for r in results] == ["downloaded"]
    assert results[0].task.slug == "pzz_944"
    assert results[0].error is None
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.MANUAL  # принято через recover
    assert len(store.files_for_version(seeded["not_found"])) == 1
    # pending-задача с пустым inbox осталась нетронутой
    still_pending = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="592",
        version_date="2021-04-21",
    )
    assert still_pending is not None
    assert still_pending.fetch_status is FetchStatus.PENDING


def test_recover_pending_only_filter(
    store: DocumentStore, seeded: dict[str, int]
) -> None:
    home = store.db_path.parent
    _seed_inbox(home, "pzz_944", {"2026-04-09_решение.pdf": _pdf_bytes()})
    _seed_inbox(home, "pzz_592", {"2021-04-21_решение.pdf": _pdf_bytes()})

    results = recover_pending(store, only=("pzz_592",), geodocs_home=home)

    assert [r.task.slug for r in results] == ["pzz_592"]
    untouched = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert untouched is not None
    assert untouched.fetch_status is FetchStatus.NOT_FOUND


def test_recover_pending_gate_rejects_broken_files(
    store: DocumentStore, seeded: dict[str, int]
) -> None:
    home = store.db_path.parent
    _seed_inbox(home, "pzz_944", {"2026-04-09_решение.pdf": b"tiny"})

    results = recover_pending(store, geodocs_home=home)

    assert [r.status for r in results] == ["gate_failed"]
    assert results[0].error
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND


def test_cli_recover_via_main(
    store: DocumentStore,
    seeded: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = store.db_path.parent
    _seed_inbox(home, "pzz_944", {"2026-04-09_решение.pdf": _pdf_bytes()})
    monkeypatch.setenv("GEODOCS_HOME", str(home))

    assert main(["recover"]) == 0

    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED


def test_record_agent_fetch_source_url_none(
    store: DocumentStore, tmp_path: Path
) -> None:
    """URL неизвестен (recover): версия принята, source_url сохранён, provenance без hermes-agent."""
    version_id = store.register_ref(_ref("944", AMENDMENT_DATE))
    store.set_fetch_status(version_id, FetchStatus.NOT_FOUND)
    store.record_agent_fetch(
        version_id,
        files=[_record(_write(tmp_path, "2026-04-09_решение.pdf", _pdf_bytes()))],
        source_url=None,
        fetched_at="2026-10-05T00:00:00+00:00",
    )
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.HERMES_AGENT
    assert version.source_url is None
    assert all(
        source != "hermes-agent" for source in store.sources_for_version(version_id)
    )

    # повторный вызов с реальным URL уже фиксирует provenance
    store.record_agent_fetch(
        version_id,
        files=[_record(_write(tmp_path, "2026-04-09_решение.pdf", _pdf_bytes()))],
        source_url="https://solreg.ru/docs/944",
        fetched_at="2026-10-05T00:00:00+00:00",
    )
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.source_url == "https://solreg.ru/docs/944"
