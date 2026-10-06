"""Тесты мульти-агентного яруса: конфиг-реестр, адаптеры, failover-цепочка.

Внешние CLI не запускаются: subprocess мокается, на уровне раннера
используются исполнители зарегистрированного тестового типа "stub".
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

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
    AgentConfigError,
    AgentTierConfig,
    ExecutionResult,
    ExecutionTimeout,
    ExecutorConfig,
    ExecutorError,
    HermesExecutor,
    QuotaExceeded,
    build_executor,
    list_pending_tasks,
    run_pending,
    run_task,
)
from geodocs.agent.cli import main
from geodocs.agent.stats import collect_stats, render_stats, stats_to_dict

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"

_INBOX_RE = re.compile(r"каталог (\S+)")


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HermesExecutor читает профиль $HERMES_HOME: изолируем от реального ~/.hermes."""
    profile = tmp_path / "hermes-src"
    profile.mkdir()
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    (profile / "auth.json").write_text("{}", encoding="utf-8")
    (profile / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))


def _pdf_bytes(size: int = 33_000) -> bytes:
    """Минимальный PDF ручной сборки, добитый до размера над порогом гейта."""
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


# ---------------------------------------------------------------------------
# Фикстуры
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
def seeded(store: DocumentStore) -> dict[str, int]:
    """Две недобранные версии: not_found и pending."""
    ids = {
        "not_found": store.register_ref(_ref("944", AMENDMENT_DATE)),
        "pending": store.register_ref(
            _ref("592", "2021-04-21", role=DocRole.BASE, amendment_number=None)
        ),
    }
    store.set_fetch_status(ids["not_found"], FetchStatus.NOT_FOUND)
    return ids


def _record(path: Path) -> FileRecord:
    return FileRecord(
        path=str(path),
        size=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        title=path.name,
    )


def _fetch_as_agent(
    store: DocumentStore,
    version_id: int,
    tmp_path: Path,
    *,
    source_url: str | None = "https://solreg.ru/docs/944",
    provider: SourceName = SourceName.HERMES_AGENT,
) -> None:
    pdf = tmp_path / "2026-04-09_решение.pdf"
    pdf.write_bytes(_pdf_bytes())
    store.record_agent_fetch(
        version_id,
        files=[_record(pdf)],
        source_url=source_url,
        fetched_at="2026-10-05T00:00:00+00:00",
        source_provider=provider,
    )


# ---------------------------------------------------------------------------
# Фиктивные исполнители: тестовый тип "stub", зарегистрированный через реестр
# ---------------------------------------------------------------------------

_STUB_BEHAVIORS: dict[str, Callable[[str, Path], ExecutionResult]] = {}
_STUB_PROVIDERS: dict[str, SourceName] = {}


class StubExecutor:
    """Исполнитель тестового типа "stub": поведение подконтрольно тесту."""

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.name = cfg.name
        self.provider = _STUB_PROVIDERS.get(cfg.name, SourceName.MANUAL)
        self.calls = 0

    def run(self, prompt: str, workdir: Path) -> ExecutionResult:
        self.calls += 1
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
    _STUB_PROVIDERS.clear()
    yield
    executors_module._EXECUTOR_TYPES.clear()
    executors_module._EXECUTOR_TYPES.update(snapshot)
    _STUB_BEHAVIORS.clear()
    _STUB_PROVIDERS.clear()


def _stub(
    name: str,
    provider: SourceName,
    behavior: Callable[[str, Path], ExecutionResult],
) -> StubExecutor:
    """Строит исполнитель зарегистрированного тестового типа "stub"."""
    _STUB_PROVIDERS[name] = provider
    _STUB_BEHAVIORS[name] = behavior
    return cast(
        StubExecutor,
        build_executor(
            ExecutorConfig(
                name=name,
                type="stub",
                command="stub",
                args=[],
                timeout_seconds=60,
                quota_patterns=[],
            )
        ),
    )


def _download_behavior(prompt: str, workdir: Path) -> ExecutionResult:
    """«Скачивает» валидный PDF в inbox из промта и печатает MANIFEST."""
    match = _INBOX_RE.search(prompt)
    assert match is not None
    inbox = Path(match.group(1))
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "2026-04-09_решение.pdf").write_bytes(_pdf_bytes())
    manifest = {
        "status": "found",
        "files": ["2026-04-09_решение.pdf"],
        "source_url": "https://solreg.ru/docs/944",
    }
    return ExecutionResult(
        stdout="шаг 1\n"
        + MANIFEST_MARKER
        + " "
        + json.dumps(manifest, ensure_ascii=False),
        stderr="",
        returncode=0,
        duration_seconds=0.1,
    )


def _failing_behavior(error: Exception) -> Callable[[str, Path], ExecutionResult]:
    def behavior(prompt: str, workdir: Path) -> ExecutionResult:
        raise error

    return behavior


def _downloader(name: str, provider: SourceName) -> StubExecutor:
    return _stub(name, provider, _download_behavior)


def _single_pass(chain: list[str], **overrides: Any) -> AgentTierConfig:
    values: dict[str, Any] = {
        "chain": chain,
        "executors": {},
        "retry_attempts": 1,
        "retry_pause_seconds": 0,
    }
    values.update(overrides)
    return AgentTierConfig(**values)


# ---------------------------------------------------------------------------
# Конфиг-реестр agents.yaml
# ---------------------------------------------------------------------------


def test_default_config_without_file(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    from geodocs.agent import load_config

    cfg = load_config(home)  # файла agents.yaml нет
    assert cfg.chain == ["hermes"]
    assert list(cfg.executors) == ["hermes"]
    assert cfg.workers == 1
    assert cfg.retry_attempts == 2
    assert cfg.retry_pause_seconds == 60
    assert cfg.run_after_sync is False
    hermes = cfg.executors["hermes"]
    assert (hermes.type, hermes.command, hermes.args, hermes.timeout_seconds) == (
        "hermes",
        "hermes",
        ["-z"],
        2400,
    )
    assert any("额度" in pattern for pattern in hermes.quota_patterns)


def test_load_agents_yaml(
    home: Path, monkeypatch: pytest.MonkeyPatch, stub_type: Iterator[None]
) -> None:
    from geodocs.agent import load_config

    (home / "agents.yaml").write_text(
        yaml.dump(
            {
                "defaults": {
                    "workers": 3,
                    "retry_attempts": 4,
                    "retry_pause_seconds": 5,
                },
                "run_after_sync": True,
                "chain": ["reserve", "hermes"],
                "executors": {
                    "hermes": {
                        "type": "hermes",
                        "command": "/opt/hermes",
                        "args": ["-z"],
                        "timeout_seconds": 30,
                        "quota_patterns": ["(?i)quota"],
                    },
                    "reserve": {
                        "type": "stub",
                        "timeout_seconds": 60,
                        "quota_patterns": ["429"],
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    cfg = load_config(home)
    assert cfg.workers == 3
    assert cfg.retry_attempts == 4
    assert cfg.retry_pause_seconds == 5
    assert cfg.run_after_sync is True
    assert cfg.chain == ["reserve", "hermes"]
    assert cfg.executors["hermes"].command == "/opt/hermes"
    assert cfg.executors["hermes"].timeout_seconds == 30
    reserve = cfg.executors["reserve"]
    assert (reserve.type, reserve.command, reserve.timeout_seconds) == (
        "stub",
        "stub",
        60,
    )
    assert reserve.quota_patterns == ["429"]


def test_config_env_override(
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_type: Iterator[None],
) -> None:
    from geodocs.agent import load_config

    override = tmp_path / "elsewhere.yaml"
    override.write_text(
        yaml.dump({"chain": ["solo"], "executors": {"solo": {"type": "stub"}}}),
        encoding="utf-8",
    )
    (home / "agents.yaml").write_text("chain: [hermes]\n", encoding="utf-8")
    monkeypatch.setenv("GEODOCS_AGENTS_CONFIG", str(override))
    cfg = load_config(home)
    assert cfg.chain == ["solo"]
    assert list(cfg.executors) == ["solo"]


def test_config_broken_yaml_reports_path(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geodocs.agent import load_config

    bad = home / "agents.yaml"
    bad.write_text("executors: [не mapping\n  : бито", encoding="utf-8")
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    with pytest.raises(AgentConfigError) as excinfo:
        load_config(home)
    assert str(bad) in str(excinfo.value)


def test_config_unknown_type(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from geodocs.agent import load_config

    (home / "agents.yaml").write_text(
        yaml.dump(
            {
                "chain": ["claude"],
                "executors": {"claude": {"type": "claude", "command": "claude"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    with pytest.raises(AgentConfigError, match="неизвестный type"):
        load_config(home)


def test_config_chain_hole(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from geodocs.agent import load_config

    (home / "agents.yaml").write_text(
        yaml.dump(
            {"chain": ["hermes", "ghost"], "executors": {"hermes": {"type": "hermes"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    with pytest.raises(AgentConfigError, match="ghost"):
        load_config(home)


def test_register_executor_type_rejects_duplicates(
    stub_type: Iterator[None],
) -> None:
    from geodocs.agent.executors import register_executor_type

    with pytest.raises(ValueError, match="уже зарегистрирован"):
        register_executor_type("stub", StubExecutor)
    with pytest.raises(ValueError, match="уже зарегистрирован"):
        register_executor_type("hermes", StubExecutor)


def test_executor_registry_restored_between_tests() -> None:
    """stub-тип тестовой фикстуры не должен протекать в соседние тесты."""
    from geodocs.agent.executors import executor_type_names

    assert "stub" not in executor_type_names()


# ---------------------------------------------------------------------------
# Адаптеры исполнителей (subprocess мокается)
# ---------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _hermes_cfg(**overrides: Any) -> ExecutorConfig:
    values: dict[str, Any] = {
        "name": "hermes",
        "type": "hermes",
        "command": "hermes",
        "args": ["-z"],
        "timeout_seconds": 2400,
        "quota_patterns": ["(?i)quota", "429"],
    }
    values.update(overrides)
    return ExecutorConfig(**values)


_PACKAGE_SKILLS = (
    "meganorm-search",
    "cntd-search",
    "document-requisites",
    "municipal-navigation",
)


def test_hermes_executor_invokes_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return _completed(stdout="ok")

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = HermesExecutor(_hermes_cfg())
    result = executor.run("промт", tmp_path)
    argv = calls[0]["argv"]
    assert argv[0] == "hermes"
    assert argv[-1] == "промт"
    assert "--accept-hooks" in argv
    assert "--skills" in argv  # harness: пакетные скилы
    assert argv.index("-z") > argv.index("--skills")  # флаги до args и промта
    # harness: изолированный HERMES_HOME с MCP-конфигом
    # (в промте нет «каталог …» → inbox = workdir/"inbox", slug = "inbox")
    hermes_home = tmp_path / ".hermes-home" / "inbox"
    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    server = config["mcp_servers"]["geodocs"]
    assert server["enabled"] is True
    assert server["args"] == ["-m", "geodocs.agent.mcp"]
    assert server["env"]["GEODOCS_AGENT_INBOX"] == str(tmp_path / "inbox")
    assert calls[0]["env"]["HERMES_HOME"] == str(hermes_home)
    for skill in _PACKAGE_SKILLS:
        assert (hermes_home / "skills" / skill / "SKILL.md").is_file()
    assert Path(calls[0]["cwd"]) == tmp_path
    assert calls[0]["capture_output"] is True
    assert result.returncode == 0 and result.stdout == "ok"
    assert result.duration_seconds >= 0


def test_executor_quota_raises_on_failure_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "geodocs.agent.executors.subprocess.run",
        lambda *a, **k: _completed(returncode=1, stderr="429 Too Many Requests"),
    )
    executor = HermesExecutor(_hermes_cfg())
    with pytest.raises(QuotaExceeded):
        executor.run("промт", tmp_path)


def test_executor_quota_patterns_configurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Свой паттерн ловит квоту, чужой — нет: квота полностью конфигурируема."""
    monkeypatch.setattr(
        "geodocs.agent.executors.subprocess.run",
        lambda *a, **k: _completed(returncode=2, stderr="Лимит исчерпан полностью"),
    )
    with pytest.raises(QuotaExceeded):
        HermesExecutor(_hermes_cfg(quota_patterns=["(?i)лимит исчерпан"])).run(
            "промт", tmp_path
        )
    # а дефолтные паттерны этого вывода не ловят — будет обычный результат с кодом 2
    result = HermesExecutor(_hermes_cfg()).run("промт", tmp_path)
    assert result.returncode == 2


def test_executor_no_quota_check_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Успешный запуск с «429» в MANIFEST (напр. № 429) — НЕ квота."""
    manifest = MANIFEST_MARKER + ' {"status": "found", "files": ["приказ_429.pdf"]}'
    monkeypatch.setattr(
        "geodocs.agent.executors.subprocess.run",
        lambda *a, **k: _completed(stdout=manifest),
    )
    executor = HermesExecutor(_hermes_cfg())
    result = executor.run("промт", tmp_path)
    assert result.returncode == 0


def test_executor_timeout_is_not_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="hermes", timeout=2400, output="молчал")

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = HermesExecutor(_hermes_cfg())
    with pytest.raises(ExecutionTimeout):
        executor.run("промт", tmp_path)


def test_executor_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*a: Any, **k: Any) -> Any:
        raise OSError("No such file or directory")

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = HermesExecutor(_hermes_cfg())
    with pytest.raises(ExecutorError, match="hermes"):
        executor.run("промт", tmp_path)


def test_build_executor_unknown_type() -> None:
    cfg = _hermes_cfg(name="weird", type="claude")
    with pytest.raises(AgentConfigError, match="неизвестный type"):
        build_executor(cfg)


def test_subprocess_executor_provider_mapping() -> None:
    assert HermesExecutor(_hermes_cfg()).provider is SourceName.HERMES_AGENT
    built = build_executor(_hermes_cfg())
    assert isinstance(built, HermesExecutor)
    assert built.provider is SourceName.HERMES_AGENT


# ---------------------------------------------------------------------------
# Failover-цепочка в раннере
# ---------------------------------------------------------------------------


def test_failover_order_and_source_provider(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    stub_type: Iterator[None],
) -> None:
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    hermes = _stub(
        "hermes",
        SourceName.HERMES_AGENT,
        _failing_behavior(QuotaExceeded("hermes", "429")),
    )
    backup = _downloader("backup", SourceName.HERMES_AGENT)
    config = _single_pass(["hermes", "backup"])

    result = run_task(
        task, store, executors=[hermes, backup], config=config, geodocs_home=home
    )

    assert result.status == "downloaded"
    assert result.executor == "backup"
    assert result.quota_exhausted == ["hermes"]
    assert hermes.calls == 1 and backup.calls == 1
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.source_provider is SourceName.HERMES_AGENT
    assert ("hermes-agent", "https://solreg.ru/docs/944") in (
        store.source_refs_for_version(task.version_id)
    )


def test_quota_exhausted_skipped_for_rest_of_run(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    stub_type: Iterator[None],
) -> None:
    """Первый исполнитель упёрся в квоту на первой задаче — дальше его не звать."""
    hermes = _stub(
        "hermes",
        SourceName.HERMES_AGENT,
        _failing_behavior(QuotaExceeded("hermes", "(?i)quota")),
    )
    backup = _downloader("backup", SourceName.HERMES_AGENT)
    config = _single_pass(["hermes", "backup"])

    results = run_pending(
        store,
        executors={"hermes": hermes, "backup": backup},
        config=config,
        geodocs_home=home,
    )

    assert [r.status for r in results] == ["downloaded", "downloaded"]
    assert hermes.calls == 1  # не 2: после квоты исключён из run
    assert backup.calls == 2
    # квота задачи 1 зафиксирована в её результате; задача 2 шла без квотных событий
    assert results[0].quota_exhausted == ["hermes"]
    assert results[1].quota_exhausted == []


def test_retry_attempts_with_pause(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_type: Iterator[None],
) -> None:
    """Первый проход падает, второй (после паузы) качает."""
    sleeps: list[float] = []
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", sleeps.append)
    attempts = iter([ExecutorError("временный сбой"), None])

    def flaky(prompt: str, workdir: Path) -> ExecutionResult:
        error = next(attempts)
        if error is not None:
            raise error
        return _download_behavior(prompt, workdir)

    hermes = _stub("hermes", SourceName.HERMES_AGENT, flaky)
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    config = _single_pass(["hermes"], retry_attempts=2, retry_pause_seconds=7)

    result = run_task(task, store, executors=[hermes], config=config, geodocs_home=home)

    assert result.status == "downloaded"
    assert result.attempts == 2
    assert sleeps == [7.0]  # пауза ровно один раз — между проходами
    assert hermes.calls == 2


def test_final_escalation_to_manual_required(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    stub_type: Iterator[None],
) -> None:
    """Все проходы неудачны → manual_required в result-JSON; recover показывает задачу."""
    from geodocs.agent.cli import recover_pending

    hermes = _stub(
        "hermes",
        SourceName.HERMES_AGENT,
        _failing_behavior(ExecutorError("бинарь сломан")),
    )
    backup = _stub(
        "backup",
        SourceName.MANUAL,
        _failing_behavior(QuotaExceeded("backup", "429")),
    )
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    config = _single_pass(["hermes", "backup"], retry_attempts=2)

    result = run_task(
        task, store, executors=[hermes, backup], config=config, geodocs_home=home
    )

    assert result.status == "manual_required"
    assert result.error and "бинарь сломан" in result.error
    assert result.quota_exhausted == ["backup"]
    assert hermes.calls == 2  # каждый проход: ошибка запуска — не квота
    assert backup.calls == 1  # после квоты в проходе 1 во второй его не звали
    saved = json.loads(
        (home / "agent" / "results" / f"{task.slug}.json").read_text(encoding="utf-8")
    )
    assert saved["status"] == "manual_required"
    # recover фильтрует по статусам БД: задача никуда не делась и показывается
    recovered = recover_pending(store, geodocs_home=home)
    assert recovered == []
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.NOT_FOUND


def test_all_quota_exhausted_short_circuits_retry(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_type: Iterator[None],
) -> None:
    """Оба исполнителя в квоте → дальнейшие проходы бессмысленны, без пауз."""
    sleeps: list[float] = []
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", sleeps.append)
    hermes = _stub(
        "hermes",
        SourceName.HERMES_AGENT,
        _failing_behavior(QuotaExceeded("hermes", "429")),
    )
    backup = _stub(
        "backup",
        SourceName.MANUAL,
        _failing_behavior(QuotaExceeded("backup", "429")),
    )
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    config = _single_pass(["hermes", "backup"], retry_attempts=3)

    result = run_task(
        task, store, executors=[hermes, backup], config=config, geodocs_home=home
    )

    assert result.status == "manual_required"
    assert hermes.calls == 1 and backup.calls == 1  # второй проход не начался
    assert sleeps == []


def test_workers_parallel_run(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    stub_type: Iterator[None],
) -> None:
    """workers>1: обе задачи добиты, result-JSON на месте у каждой."""
    hermes = _downloader("hermes", SourceName.HERMES_AGENT)
    config = _single_pass(["hermes"], workers=2)

    results = run_pending(
        store, executors={"hermes": hermes}, config=config, geodocs_home=home
    )

    assert [r.status for r in results] == ["downloaded", "downloaded"]
    for task_slug in ("pzz_944", "pzz_592"):
        saved = json.loads(
            (home / "agent" / "results" / f"{task_slug}.json").read_text(
                encoding="utf-8"
            )
        )
        assert saved["status"] == "downloaded"


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def test_stats_collects_providers_tasks_and_quota(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    tmp_path: Path,
) -> None:
    _fetch_as_agent(store, seeded["not_found"], tmp_path)
    results_dir = home / "agent" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "pzz_944.json").write_text(
        json.dumps(
            {
                "status": "downloaded",
                "executor": "hermes",
                "attempts": 1,
                "quota_exhausted": ["backup"],
            }
        ),
        encoding="utf-8",
    )
    (results_dir / "pzz_592.json").write_text(
        json.dumps({"status": "manual_required", "executor": "hermes", "attempts": 2}),
        encoding="utf-8",
    )

    stats = collect_stats(store, home)

    (provider,) = stats.providers
    assert provider.provider == "hermes-agent"
    assert provider.versions == 1
    assert provider.files == 1
    assert stats.status_counts == {"downloaded": 1, "manual_required": 1}
    assert stats.quota_counts == {"backup": 1}
    assert stats.manual_required == ["pzz_592"]
    text = render_stats(stats)
    assert "hermes-agent" in text and "manual_required" in text and "backup=1" in text
    as_json = stats_to_dict(stats)
    assert as_json["providers"][0]["versions"] == 1
    json.dumps(as_json)  # сериализуемо


def test_cli_stats_text_and_json(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fetch_as_agent(store, seeded["not_found"], tmp_path)
    monkeypatch.setenv("GEODOCS_HOME", str(home))

    assert main(["stats"]) == 0
    out = capsys.readouterr().out
    assert "hermes-agent" in out and "версий" in out

    assert main(["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["providers"][0]["provider"] == "hermes-agent"


# ---------------------------------------------------------------------------
# CLI run: --dry-run, --chain, --workers
# ---------------------------------------------------------------------------


def test_cli_run_dry_run(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GEODOCS_HOME", str(home))
    assert main(["run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "цепочка: hermes" in out
    assert "pzz_944" in out and "pzz_592" in out
    assert "2 проходов" in out
    # ничего не запустилось: result-JSON нет, задачи не тронуты
    assert not (home / "agent" / "results").exists()
    assert len(list_pending_tasks(store)) == 2


def test_cli_run_dry_run_chain_override(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (home / "agents.yaml").write_text(
        yaml.dump(
            {
                "chain": ["hermes", "reserve"],
                "executors": {
                    "hermes": {"type": "hermes"},
                    "reserve": {"type": "hermes", "command": "hermes-reserve"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GEODOCS_HOME", str(home))
    assert main(["run", "--dry-run", "--chain", "reserve"]) == 0
    out = capsys.readouterr().out
    assert "цепочка: reserve" in out

    assert main(["run", "--dry-run", "--chain", "ghost"]) == 2
    assert "ghost" in capsys.readouterr().err


def test_cli_run_dry_run_workers_override(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GEODOCS_HOME", str(home))
    assert main(["run", "--dry-run", "--workers", "4"]) == 0
    assert "workers=4" in capsys.readouterr().out


def test_run_after_sync_flag_is_parsed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geodocs.agent import load_config

    (home / "agents.yaml").write_text(
        "run_after_sync: true\nchain: [hermes]\nexecutors:\n  hermes:\n    type: hermes\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    assert load_config(home).run_after_sync is True


def test_cli_run_end_to_end_via_agents_yaml(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Полный путь: agents.yaml со сценарием-заглушкой → `geodocs-agent run`."""
    monkeypatch.setenv("GEODOCS_HOME", str(home))
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", lambda _s: None)

    first = list_pending_tasks(store)[0]
    script = tmp_path / "fake_agent.sh"
    script.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        f"mkdir -p {home}/inbox/{first.slug}\n"
        f"cp {tmp_path}/doc.pdf {home}/inbox/{first.slug}/\n"
        + "echo '"
        + MANIFEST_MARKER
        + " "
        + json.dumps(
            {
                "status": "found",
                "files": ["doc.pdf"],
                "source_url": "https://solreg.ru/docs/944",
            },
            ensure_ascii=False,
        )
        + "'\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    (tmp_path / "doc.pdf").write_bytes(_pdf_bytes())

    (home / "agents.yaml").write_text(
        yaml.dump(
            {
                "defaults": {"retry_attempts": 1, "retry_pause_seconds": 0},
                "chain": ["hermes"],
                "executors": {
                    "hermes": {
                        "type": "hermes",
                        "command": str(script),
                        "args": [],
                        "timeout_seconds": 60,
                        "quota_patterns": [],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    assert main(["run", "--limit", "1", "--status", "not_found"]) == 0
    out = capsys.readouterr().out
    assert "скачано 1" in out
    saved = json.loads(
        (home / "agent" / "results" / f"{first.slug}.json").read_text(encoding="utf-8")
    )
    assert saved["status"] == "downloaded"
    assert saved["executor"] == "hermes"
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.HERMES_AGENT
