"""Исполнители агентного яруса: сменные адаптеры над CLI-агентами.

Каждый исполнитель — тонкая обёртка над one-shot запуском LLM-агента по
промту с захватом stdout/stderr. Исчерпание квоты (паттерны из конфига)
поднимает QuotaExceeded — сигнал failover-цепочке переключиться на
следующего исполнителя.

HermesExecutor — harness со статическими инструментами: на задачу он
материализует изолированный HERMES_HOME (config.yaml с mcp_servers.geodocs
— stdio-сервер `geodocs.agent.mcp` с инструментами порталов, ссылки на
auth.json/.env основного профиля, копии пакетных скилов) и запускает
`hermes -z`; динамический поиск — штатные веб-инструменты Hermes (Firecrawl).

Новый агент добавляется без правок этого модуля:

    class MyAgentExecutor(SubprocessExecutor):
        provider = SourceName.MANUAL  # или свой SourceName для provenance в БД

    register_executor_type("myagent", MyAgentExecutor)

После регистрации тип доступен в agents.yaml (`type: myagent`), а цепочка,
failover, retry и квоты работают без изменений.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import yaml

from ..models import SourceName
from .config import AgentConfigError, ExecutorConfig

_TAIL_LEN = 400
_PACKAGE_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# Шаблон промта: «СКАЧИВАНИЕ: каталог {inbox_dir} (mkdir -p)» — inbox задачи.
_INBOX_PROMPT_RE = re.compile(r"каталог (\S+)")
_HERMES_HOME_ENV = "HERMES_HOME"


class QuotaExceeded(RuntimeError):
    """Исполнитель исчерпал LLM-квоту: в его выводе матчится quota pattern."""

    def __init__(self, executor: str, detail: str) -> None:
        super().__init__(f"{executor}: квота исчерпана: {detail}")
        self.executor = executor
        self.detail = detail


class ExecutionTimeout(RuntimeError):
    """Исполнитель не уложился в timeout_seconds (это НЕ квота)."""


class ExecutorError(RuntimeError):
    """Исполнитель не запустился (бинарь не найден и т.п.)."""


@dataclass(frozen=True)
class ExecutionResult:
    """Завершившийся запуск исполнителя: вывод, код возврата, длительность."""

    stdout: str
    stderr: str
    returncode: int
    duration_seconds: float


@runtime_checkable
class Executor(Protocol):
    """Сменный исполнитель one-shot запуска LLM-агента по промту."""

    name: str
    provider: SourceName

    def run(self, prompt: str, workdir: Path) -> ExecutionResult:
        """Запускает агента; QuotaExceeded/ExecutionTimeout/ExecutorError при провале."""
        ...


def _tail(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-_TAIL_LEN:]


class SubprocessExecutor:
    """Базовый исполнитель: subprocess без shell, cwd=workdir, capture output."""

    provider: SourceName = SourceName.MANUAL

    def __init__(self, cfg: ExecutorConfig) -> None:
        self.name = cfg.name
        self._command = cfg.command
        self._args = list(cfg.args)
        self._timeout = cfg.timeout_seconds
        self._quota_patterns = cfg.compiled_quota_patterns()

    def _quota_hit(self, output: str) -> str | None:
        """Первый сматчившийся паттерн квоты или None."""
        for pattern in self._quota_patterns:
            if pattern.search(output):
                return pattern.pattern
        return None

    def run(self, prompt: str, workdir: Path) -> ExecutionResult:
        self._materialize(prompt, workdir)
        argv = self._argv(prompt, workdir)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,  # код возврата разбираем сами ниже
                env=self._subprocess_env(workdir),
            )
        except subprocess.TimeoutExpired as exc:
            output = _tail(exc.stdout) + _tail(exc.stderr)
            hit = self._quota_hit(output)
            if hit:
                raise QuotaExceeded(
                    self.name, f"по выводу убитого таймаутом процесса: {hit}"
                )
            raise ExecutionTimeout(
                f"{self.name} не уложился в {self._timeout} с; вывод:"
                f" {_tail(exc.stdout) or _tail(exc.stderr) or 'пусто'}"
            ) from exc
        except OSError as exc:
            raise ExecutorError(
                f"не удалось запустить {shlex.join(argv[: len(argv) - 1]) or self._command}: {exc}"
            ) from exc
        duration = time.monotonic() - started
        if proc.returncode != 0:
            # Квота диагностируется только по выводу неуспешного запуска:
            # удачный stdout содержит MANIFEST с номерами документов вида «429».
            hit = self._quota_hit(proc.stdout + proc.stderr)
            if hit:
                raise QuotaExceeded(self.name, hit)
        return ExecutionResult(
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            duration_seconds=duration,
        )

    # Хуки для адаптеров с материализацией окружения (см. HermesExecutor).
    def _materialize(self, prompt: str, workdir: Path) -> None:
        """Подготовка рабочего каталога перед запуском; база — ничего."""

    def _extra_argv(self, prompt: str, workdir: Path) -> list[str]:
        """Дополнительные флаги argv перед промтом; база — ничего."""
        return []

    def _subprocess_env(self, workdir: Path) -> dict[str, str] | None:
        """Окружение subprocess; None — наследовать os.environ."""
        return None

    def _argv(self, prompt: str, workdir: Path) -> list[str]:
        # Флаги (--skills) — ДО промта: позиционный промт должен быть последним.
        return [self._command, *self._extra_argv(prompt, workdir), *self._args, prompt]


def _inbox_from_prompt(prompt: str, workdir: Path) -> Path:
    """Inbox задачи — из шаблона промта «каталог {inbox_dir}»."""
    match = _INBOX_PROMPT_RE.search(prompt)
    if match:
        return Path(match.group(1))
    return workdir / "inbox"


def _hermes_source_home() -> Path:
    """Основной профиль Hermes: источник auth.json/.env/config.yaml."""
    return Path(os.environ.get(_HERMES_HOME_ENV, str(Path.home() / ".hermes")))


class HermesExecutor(SubprocessExecutor):
    """Hermes Agent one-shot (`hermes -z`) с harness статических инструментов.

    На каждый запуск материализует изолированный HERMES_HOME
    (`<workdir>/.hermes-home/<slug>`, slug — имя inbox задачи, чтобы
    параллельные задачи не делили один каталог):

    - `config.yaml` — копия config.yaml основного профиля с пропатченным
      `mcp_servers.geodocs` (stdio-сервер `geodocs.agent.mcp`, inbox задачи
      из промта); без основного конфига — минимальный (только MCP);
    - `auth.json` / `.env` — симлинки на основной профиль (креды провайдера
      и ключи вроде FIRECRAWL_API_KEY не копируются в репозиторий);
    - `skills/` — копии пакетных скилов и дополнительных из конфигурации.

    Динамический поиск — штатные веб-инструменты Hermes (Firecrawl и др.),
    статический — MCP-инструменты порталов.
    """

    provider = SourceName.HERMES_AGENT

    def __init__(self, cfg: ExecutorConfig) -> None:
        super().__init__(cfg)
        self._cfg = cfg
        self._extra_skills_dirs = list(cfg.skills_dirs)
        self._mcp_enabled = cfg.mcp
        self._home: Path | None = None

    def _materialize(self, prompt: str, workdir: Path) -> None:
        inbox = _inbox_from_prompt(prompt, workdir)
        home = workdir / ".hermes-home" / (inbox.name or "task")
        home.mkdir(parents=True, exist_ok=True)
        if self._mcp_enabled:
            self._write_config(home, inbox)
        self._link_credentials(home)
        self._copy_skills(home)
        self._home = home

    def _extra_argv(self, prompt: str, workdir: Path) -> list[str]:
        argv = ["--accept-hooks"]
        names = self._skill_names()
        if names:
            argv.extend(["--skills", ",".join(names)])
        return argv

    def _subprocess_env(self, workdir: Path) -> dict[str, str] | None:
        if self._home is None:
            return None
        return {**os.environ, _HERMES_HOME_ENV: str(self._home)}

    def _write_config(self, home: Path, inbox: Path) -> None:
        geodocs_home = self._cfg.home or Path(
            os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs"))
        )
        source_config = _hermes_source_home() / "config.yaml"
        config: dict = {}
        if source_config.is_file():
            try:
                loaded = yaml.safe_load(source_config.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            except yaml.YAMLError:
                config = {}  # битый конфиг профиля — минимальный, только MCP
        servers = config.get("mcp_servers")
        if not isinstance(servers, dict):
            servers = {}
        servers["geodocs"] = {
            "enabled": True,
            "command": sys.executable,
            "args": ["-m", "geodocs.agent.mcp"],
            "env": {
                "GEODOCS_HOME": str(geodocs_home),
                "GEODOCS_AGENT_INBOX": str(inbox),
            },
        }
        config["mcp_servers"] = servers
        (home / "config.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8"
        )

    def _link_credentials(self, home: Path) -> None:
        source = _hermes_source_home()
        for name in ("auth.json", ".env"):
            target = source / name
            link = home / name
            if link.is_symlink() or link.exists():
                continue
            if target.is_file():
                try:
                    link.symlink_to(target)
                except OSError:
                    pass  # креды недоступны — агент упадёт с ошибкой провайдера

    def _skill_dirs(self) -> list[Path]:
        return [
            d
            for d in (_PACKAGE_SKILLS_DIR, *self._extra_skills_dirs)
            if Path(d).is_dir()
        ]

    def _copy_skills(self, home: Path) -> None:
        skills_root = home / "skills"
        for source_dir in self._skill_dirs():
            for skill in sorted(Path(source_dir).iterdir()):
                if not (skill / "SKILL.md").is_file():
                    continue
                target = skills_root / skill.name
                if target.exists():
                    continue
                try:
                    shutil.copytree(skill, target)
                except OSError:
                    pass  # скил не скопировался — промт отработает без него

    def _skill_names(self) -> list[str]:
        names: list[str] = []
        for source_dir in self._skill_dirs():
            for skill in sorted(Path(source_dir).iterdir()):
                if (skill / "SKILL.md").is_file():
                    names.append(skill.name)
        return names


_EXECUTOR_TYPES: dict[str, Callable[[ExecutorConfig], Executor]] = {}


def register_executor_type(
    type_name: str, factory: Callable[[ExecutorConfig], Executor]
) -> None:
    """Регистрирует адаптер исполнителя; перезапись зарегистрированного имени — ValueError."""
    if type_name in _EXECUTOR_TYPES:
        raise ValueError(f"тип исполнителя {type_name!r} уже зарегистрирован")
    _EXECUTOR_TYPES[type_name] = factory


def executor_type_names() -> tuple[str, ...]:
    """Имена зарегистрированных типов (валидация конфига, сообщения об ошибках)."""
    return tuple(_EXECUTOR_TYPES)


def build_executor(cfg: ExecutorConfig) -> Executor:
    """Фабрика адаптера по записи конфига; неизвестный type — ошибка конфига."""
    factory = _EXECUTOR_TYPES.get(cfg.type)
    if factory is None:
        known = ", ".join(executor_type_names()) or "нет зарегистрированных"
        raise AgentConfigError(
            f"исполнитель {cfg.name!r}: неизвестный type {cfg.type!r}"
            f" (зарегистрированные: {known})"
        )
    return factory(cfg)


register_executor_type("hermes", HermesExecutor)
