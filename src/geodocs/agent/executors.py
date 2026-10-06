"""Исполнители агентного яруса: сменные адаптеры над CLI-агентами.

Каждый исполнитель — тонкая обёртка над one-shot запуском LLM-агента по
промту с захватом stdout/stderr. Исчерпание квоты (паттерны из конфига)
поднимает QuotaExceeded — сигнал failover-цепочке переключиться на
следующего исполнителя.

KimiExecutor — harness со статическими инструментами: перед запуском он
материализует в рабочем каталоге `.kimi-code/mcp.json` (stdio-сервер
`geodocs.agent.mcp` с инструментами порталов) и гарантирует workspace-trust
для этого каталога, а в argv добавляет `--skills-dir` с пакетными скилами.

Новый агент добавляется без правок этого модуля:

    class MyAgentExecutor(SubprocessExecutor):
        provider = SourceName.MANUAL  # или свой SourceName для provenance в БД

    register_executor_type("myagent", MyAgentExecutor)

После регистрации тип доступен в agents.yaml (`type: myagent`), а цепочка,
failover, retry и квоты работают без изменений.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import SourceName
from .config import AgentConfigError, ExecutorConfig

_TAIL_LEN = 400
_PACKAGE_SKILLS_DIR = Path(__file__).resolve().parent / "skills"
# Шаблон промта: «СКАЧИВАНИЕ: каталог {inbox_dir} (mkdir -p)» — inbox задачи.
_INBOX_PROMPT_RE = re.compile(r"каталог (\S+)")
_KIMI_HOME_ENV = "KIMI_HOME"


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

    # Хуки для адаптеров с материализацией окружения (см. KimiExecutor).
    def _materialize(self, prompt: str, workdir: Path) -> None:
        """Подготовка рабочего каталога перед запуском; база — ничего."""

    def _extra_argv(self, prompt: str, workdir: Path) -> list[str]:
        """Дополнительные флаги argv перед промтом; база — ничего."""
        return []

    def _argv(self, prompt: str, workdir: Path) -> list[str]:
        # Флаги (--skills-dir) — ДО -p: иначе commander съедает их как значение -p.
        return [self._command, *self._extra_argv(prompt, workdir), *self._args, prompt]


class KimiExecutor(SubprocessExecutor):
    """Kimi CLI one-shot с harness статических инструментов.

    Перед запуском: `.kimi-code/mcp.json` (stdio MCP `geodocs.agent.mcp`,
    inbox задачи из промта) + workspace-trust рабочего каталога; в argv —
    `--skills-dir` пакетных скилов и дополнительных из конфигурации.
    """

    provider = SourceName.KIMI_AGENT

    def __init__(self, cfg: ExecutorConfig) -> None:
        super().__init__(cfg)
        self._cfg = cfg
        self._extra_skills_dirs = list(cfg.skills_dirs)
        self._mcp_enabled = cfg.mcp

    def _materialize(self, prompt: str, workdir: Path) -> None:
        if not self._mcp_enabled:
            return
        inbox = self._inbox_from_prompt(prompt, workdir)
        self._write_mcp_config(workdir, inbox)
        _ensure_workspace_trust(workdir)

    def _extra_argv(self, prompt: str, workdir: Path) -> list[str]:
        argv: list[str] = []
        for skills_dir in self._skills_dirs():
            argv.extend(["--skills-dir", str(skills_dir)])
        return argv

    def _skills_dirs(self) -> list[Path]:
        dirs = [
            d
            for d in (_PACKAGE_SKILLS_DIR, *self._extra_skills_dirs)
            if Path(d).is_dir()
        ]
        return dirs

    def _inbox_from_prompt(self, prompt: str, workdir: Path) -> Path:
        """Inbox задачи — из шаблона промта «каталог {inbox_dir}»."""
        match = _INBOX_PROMPT_RE.search(prompt)
        if match:
            return Path(match.group(1))
        return workdir / "inbox"

    def _write_mcp_config(self, workdir: Path, inbox: Path) -> None:
        home = self._cfg.home or Path(
            os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs"))
        )
        config = {
            "mcpServers": {
                "geodocs": {
                    "command": sys.executable,
                    "args": ["-m", "geodocs.agent.mcp"],
                    "env": {
                        "GEODOCS_HOME": str(home),
                        "GEODOCS_AGENT_INBOX": str(inbox),
                    },
                }
            }
        }
        config_dir = workdir / ".kimi-code"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "mcp.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _ensure_workspace_trust(workdir: Path) -> None:
    """Файл workspace-trust Kimi CLI: без него проектный mcp.json игнорируется.

    Идентификатор каталога у Kimi: `wd_<basename-lower>_<sha256(realpath)[:12]>`
    (установлено опытным путём, kimi 2.1.1). Файл не перезаписываем, если есть.
    """
    root = Path(os.path.realpath(workdir))
    kimi_home = Path(os.environ.get(_KIMI_HOME_ENV, Path.home() / ".kimi-code"))
    trust_dir = kimi_home / "workspace-trust"
    digest = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    name = root.name.lower() or "workspace"
    trust_file = trust_dir / f"wd_{name}_{digest}"
    if trust_file.exists():
        return
    try:
        trust_dir.mkdir(parents=True, exist_ok=True)
        payload = {"root": str(root), "trustedAt": int(time.time() * 1000)}
        trust_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass  # trust нельзя записать — MCP просто не подключится, не фатально


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


register_executor_type("kimi", KimiExecutor)
