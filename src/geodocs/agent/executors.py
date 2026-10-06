"""Исполнители агентного яруса: сменные адаптеры над CLI-агентами.

Каждый исполнитель — тонкая обёртка над one-shot запуском LLM-агента по
промту с захватом stdout/stderr. Исчерпание квоты (паттерны из конфига)
поднимает QuotaExceeded — сигнал failover-цепочке переключиться на
следующего исполнителя.

Новый агент добавляется без правок этого модуля:

    class MyAgentExecutor(SubprocessExecutor):
        provider = SourceName.MANUAL  # или свой SourceName
        # при необходимости — своя логика run()

    register_executor_type("myagent", MyAgentExecutor)

После регистрации тип доступен в agents.yaml (`type: myagent`), а цепочка,
failover, retry и квоты работают без изменений.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import SourceName
from .config import AgentConfigError, ExecutorConfig

_TAIL_LEN = 400


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
        argv = [self._command, *self._args, prompt]
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


class KimiExecutor(SubprocessExecutor):
    """Kimi CLI one-shot: `kimi -p "<prompt>`."""

    provider = SourceName.KIMI_AGENT


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
