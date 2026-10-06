"""Тесты мульти-агентного яруса: конфиг-реестр, адаптеры, failover-цепочка.

Внешние CLI (kimi/hermes) не запускаются: subprocess мокается, на уровне
раннера используются фиктивные исполнители с тем же протоколом Executor.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

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
    KimiExecutor,
    QuotaExceeded,
    build_executor,
    list_pending_tasks,
    run_pending,
    run_task,
)
from geodocs.agent.cli import main
from geodocs.agent.executors import SubprocessExecutor
from geodocs.agent.stats import collect_stats, render_stats, stats_to_dict

MUNICIPALITY = "Городской округ Солнечногорск"
AMENDMENT_DATE = "2026-04-09"

_INBOX_RE = re.compile(r"каталог (\S+)")


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
    provider: SourceName = SourceName.KIMI_AGENT,
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
# Фиктивные исполнители
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Исполнитель-заглушка: script(prompt, workdir) -> ExecutionResult или error."""

    def __init__(
        self,
        name: str,
        provider: SourceName,
        script: Callable[[str, Path], ExecutionResult] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.provider = provider
        self.calls = 0
        self.prompts: list[str] = []
        self._script = script
        self._error = error

    def run(self, prompt: str, workdir: Path) -> ExecutionResult:
        self.calls += 1
        self.prompts.append(prompt)
        if self._error is not None:
            raise self._error
        assert self._script is not None
        return self._script(prompt, workdir)


def _downloader(
    name: str,
    provider: SourceName,
    *,
    source_url: str = "https://solreg.ru/docs/944",
) -> FakeExecutor:
    """Исполнитель, который «скачивает» валидный PDF в inbox из промта и печатает MANIFEST."""

    def script(prompt: str, workdir: Path) -> ExecutionResult:
        match = _INBOX_RE.search(prompt)
        assert match is not None
        inbox = Path(match.group(1))
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "2026-04-09_решение.pdf").write_bytes(_pdf_bytes())
        manifest = {
            "status": "found",
            "files": ["2026-04-09_решение.pdf"],
            "source_url": source_url,
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

    return FakeExecutor(name, provider, script=script)


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
    assert cfg.chain == ["kimi", "hermes"]
    assert cfg.workers == 1
    assert cfg.retry_attempts == 2
    assert cfg.retry_pause_seconds == 60
    assert cfg.run_after_sync is False
    kimi = cfg.executors["kimi"]
    assert (kimi.type, kimi.command, kimi.args, kimi.timeout_seconds) == (
        "kimi",
        "kimi",
        ["-p"],
        900,
    )
    hermes = cfg.executors["hermes"]
    assert (hermes.type, hermes.command, hermes.args) == ("hermes", "hermes", ["-z"])
    assert any("额度" in pattern for pattern in kimi.quota_patterns)


def test_load_agents_yaml(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                "chain": ["hermes", "kimi"],
                "executors": {
                    "kimi": {
                        "type": "kimi",
                        "command": "/opt/kimi",
                        "args": ["-p"],
                        "timeout_seconds": 30,
                        "quota_patterns": ["(?i)quota"],
                    },
                    "hermes": {
                        "type": "hermes",
                        "command": "hermes",
                        "args": ["-z"],
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
    assert cfg.chain == ["hermes", "kimi"]
    assert cfg.executors["kimi"].command == "/opt/kimi"
    assert cfg.executors["kimi"].timeout_seconds == 30
    assert cfg.executors["hermes"].quota_patterns == ["429"]


def test_config_env_override(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geodocs.agent import load_config

    override = tmp_path / "elsewhere.yaml"
    override.write_text(
        yaml.dump({"chain": ["hermes"], "executors": {"hermes": {"type": "hermes"}}}),
        encoding="utf-8",
    )
    (home / "agents.yaml").write_text("chain: [kimi]\n", encoding="utf-8")
    monkeypatch.setenv("GEODOCS_AGENTS_CONFIG", str(override))
    cfg = load_config(home)
    assert cfg.chain == ["hermes"]
    assert list(cfg.executors) == ["hermes"]


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
            {"chain": ["kimi", "ghost"], "executors": {"kimi": {"type": "kimi"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("GEODOCS_AGENTS_CONFIG", raising=False)
    with pytest.raises(AgentConfigError, match="ghost"):
        load_config(home)


# ---------------------------------------------------------------------------
# Адаптеры исполнителей (subprocess мокается)
# ---------------------------------------------------------------------------


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _kimi_cfg(**overrides: Any) -> ExecutorConfig:
    values: dict[str, Any] = {
        "name": "kimi",
        "type": "kimi",
        "command": "kimi",
        "args": ["-p"],
        "timeout_seconds": 900,
        "quota_patterns": ["(?i)quota", "429"],
    }
    values.update(overrides)
    return ExecutorConfig(**values)


def test_kimi_executor_invokes_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return _completed(stdout="ok")

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = KimiExecutor(_kimi_cfg())
    result = executor.run("промт", tmp_path)
    assert calls[0]["argv"] == ["kimi", "-p", "промт"]
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
    executor = KimiExecutor(_kimi_cfg())
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
        KimiExecutor(_kimi_cfg(quota_patterns=["(?i)лимит исчерпан"])).run(
            "промт", tmp_path
        )
    # а дефолтные паттерны этого вывода не ловят — будет обычный результат с кодом 2
    result = KimiExecutor(_kimi_cfg()).run("промт", tmp_path)
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
    executor = KimiExecutor(_kimi_cfg())
    result = executor.run("промт", tmp_path)
    assert result.returncode == 0


def test_executor_timeout_is_not_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="kimi", timeout=900, output="молчал")

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = KimiExecutor(_kimi_cfg())
    with pytest.raises(ExecutionTimeout):
        executor.run("промт", tmp_path)


def test_executor_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*a: Any, **k: Any) -> Any:
        raise OSError("No such file or directory")

    monkeypatch.setattr("geodocs.agent.executors.subprocess.run", fake_run)
    executor = KimiExecutor(_kimi_cfg())
    with pytest.raises(ExecutorError, match="kimi"):
        executor.run("промт", tmp_path)


def test_build_executor_unknown_type() -> None:
    cfg = _kimi_cfg(name="weird", type="claude")
    with pytest.raises(AgentConfigError, match="неизвестный type"):
        build_executor(cfg)


def test_subprocess_executor_provider_mapping() -> None:
    assert KimiExecutor(_kimi_cfg()).provider is SourceName.KIMI_AGENT
    hermes = build_executor(_kimi_cfg(name="hermes", type="hermes", args=["-z"]))
    assert hermes.provider is SourceName.HERMES_AGENT
    assert isinstance(hermes, SubprocessExecutor)


# ---------------------------------------------------------------------------
# Failover-цепочка в раннере
# ---------------------------------------------------------------------------


def test_failover_order_and_source_provider(
    store: DocumentStore, seeded: dict[str, int], home: Path
) -> None:
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    kimi = FakeExecutor(
        "kimi", SourceName.KIMI_AGENT, error=QuotaExceeded("kimi", "429")
    )
    hermes = _downloader("hermes", SourceName.HERMES_AGENT)
    config = _single_pass(["kimi", "hermes"])

    result = run_task(
        task, store, executors=[kimi, hermes], config=config, geodocs_home=home
    )

    assert result.status == "downloaded"
    assert result.executor == "hermes"
    assert result.quota_exhausted == ["kimi"]
    assert kimi.calls == 1 and hermes.calls == 1
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
    store: DocumentStore, seeded: dict[str, int], home: Path
) -> None:
    """Kimi упёрся в квоту на первой задаче — для второй его уже не звать."""
    kimi = FakeExecutor(
        "kimi", SourceName.KIMI_AGENT, error=QuotaExceeded("kimi", "(?i)quota")
    )
    hermes = _downloader("hermes", SourceName.HERMES_AGENT)
    config = _single_pass(["kimi", "hermes"])

    results = run_pending(
        store,
        executors={"kimi": kimi, "hermes": hermes},
        config=config,
        geodocs_home=home,
    )

    assert [r.status for r in results] == ["downloaded", "downloaded"]
    assert kimi.calls == 1  # не 2: после квоты исключён из run
    assert hermes.calls == 2
    # квота задачи 1 зафиксирована в её результате; задача 2 шла без квотных событий
    assert results[0].quota_exhausted == ["kimi"]
    assert results[1].quota_exhausted == []


def test_retry_attempts_with_pause(
    store: DocumentStore,
    seeded: dict[str, int],
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Первый проход падает, второй (после паузы) качает."""
    sleeps: list[float] = []
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", sleeps.append)
    attempts = iter([ExecutorError("временный сбой"), None])

    def flaky(prompt: str, workdir: Path) -> ExecutionResult:
        error = next(attempts)
        if error is not None:
            raise error
        return _downloader("kimi", SourceName.KIMI_AGENT)._script(prompt, workdir)  # type: ignore[misc]

    kimi = FakeExecutor("kimi", SourceName.KIMI_AGENT, script=flaky)
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    config = _single_pass(["kimi"], retry_attempts=2, retry_pause_seconds=7)

    result = run_task(task, store, executors=[kimi], config=config, geodocs_home=home)

    assert result.status == "downloaded"
    assert result.attempts == 2
    assert sleeps == [7.0]  # пауза ровно один раз — между проходами
    assert kimi.calls == 2


def test_final_escalation_to_manual_required(
    store: DocumentStore, seeded: dict[str, int], home: Path
) -> None:
    """Все проходы неудачны → manual_required в result-JSON; recover показывает задачу."""
    from geodocs.agent.cli import recover_pending

    kimi = FakeExecutor(
        "kimi", SourceName.KIMI_AGENT, error=ExecutorError("бинарь сломан")
    )
    hermes = FakeExecutor(
        "hermes", SourceName.HERMES_AGENT, error=QuotaExceeded("hermes", "429")
    )
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    config = _single_pass(["kimi", "hermes"], retry_attempts=2)

    result = run_task(
        task, store, executors=[kimi, hermes], config=config, geodocs_home=home
    )

    assert result.status == "manual_required"
    assert result.error and "бинарь сломан" in result.error
    assert result.quota_exhausted == ["hermes"]
    assert kimi.calls == 2  # каждый проход: ошибка запуска — не квота
    assert hermes.calls == 1  # после квоты в проходе 1 во второй его не звали
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
) -> None:
    """Оба исполнителя в квоте → дальнейшие проходы бессмысленны, без пауз."""
    sleeps: list[float] = []
    monkeypatch.setattr("geodocs.agent.runner.time.sleep", sleeps.append)
    kimi = FakeExecutor(
        "kimi", SourceName.KIMI_AGENT, error=QuotaExceeded("kimi", "429")
    )
    hermes = FakeExecutor(
        "hermes", SourceName.HERMES_AGENT, error=QuotaExceeded("hermes", "429")
    )
    task = list_pending_tasks(store, statuses=("not_found",))[0]
    config = _single_pass(["kimi", "hermes"], retry_attempts=3)

    result = run_task(
        task, store, executors=[kimi, hermes], config=config, geodocs_home=home
    )

    assert result.status == "manual_required"
    assert kimi.calls == 1 and hermes.calls == 1  # второй проход не начался
    assert sleeps == []


def test_workers_parallel_run(
    store: DocumentStore, seeded: dict[str, int], home: Path
) -> None:
    """workers>1: обе задачи добиты, result-JSON на месте у каждой."""
    kimi = _downloader("kimi", SourceName.KIMI_AGENT)
    config = _single_pass(["kimi"], workers=2)

    results = run_pending(
        store, executors={"kimi": kimi}, config=config, geodocs_home=home
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
                "executor": "kimi",
                "attempts": 1,
                "quota_exhausted": ["hermes"],
            }
        ),
        encoding="utf-8",
    )
    (results_dir / "pzz_592.json").write_text(
        json.dumps({"status": "manual_required", "executor": "kimi", "attempts": 2}),
        encoding="utf-8",
    )

    stats = collect_stats(store, home)

    (provider,) = stats.providers
    assert provider.provider == "kimi-agent"
    assert provider.versions == 1
    assert provider.files == 1
    assert stats.status_counts == {"downloaded": 1, "manual_required": 1}
    assert stats.quota_counts == {"hermes": 1}
    assert stats.manual_required == ["pzz_592"]
    text = render_stats(stats)
    assert "kimi-agent" in text and "manual_required" in text and "hermes=1" in text
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
    assert "kimi-agent" in out and "версий" in out

    assert main(["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["providers"][0]["provider"] == "kimi-agent"


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
    assert "kimi → hermes" in out
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
    monkeypatch.setenv("GEODOCS_HOME", str(home))
    assert main(["run", "--dry-run", "--chain", "hermes"]) == 0
    out = capsys.readouterr().out
    assert "hermes" in out and "kimi" not in out

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
        "run_after_sync: true\nchain: [kimi]\nexecutors:\n  kimi:\n    type: kimi\n",
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
                "chain": ["kimi", "hermes"],
                "executors": {
                    "kimi": {
                        "type": "kimi",
                        "command": str(script),
                        "args": [],
                        "timeout_seconds": 60,
                        "quota_patterns": [],
                    },
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
    assert saved["executor"] == "kimi"
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number="944",
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.fetch_status is FetchStatus.DOWNLOADED
    assert version.source_provider is SourceName.KIMI_AGENT
