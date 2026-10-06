"""Конфиг-реестр исполнителей агентного яруса: $GEODOCS_HOME/agents.yaml.

Файл опционален: без него используются встроенные дефолты (chain из hermes).
Путь к файлу переопределяется env `GEODOCS_AGENTS_CONFIG`. Процессы
короткоживущие, поэтому конфиг перечитывается при каждом запуске CLI.
Дополнительные типы исполнителей регистрируются кодом
(`geodocs.agent.executors.register_executor_type`) и доступны в agents.yaml
сразу после регистрации.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_CONFIG = "GEODOCS_AGENTS_CONFIG"
_CONFIG_FILENAME = "agents.yaml"

_DEFAULT_QUOTA_PATTERNS = [
    "(?i)quota",
    "(?i)rate.?limit",
    "(?i)limit exceeded",
    "429",
    "额度",
]

_BUILTIN_DEFAULTS: dict[str, Any] = {
    "defaults": {
        "workers": 1,
        "retry_attempts": 2,
        "retry_pause_seconds": 60,
    },
    "chain": ["hermes"],
    "executors": {
        "hermes": {
            "type": "hermes",
            "command": "hermes",
            "args": ["-z"],
            "timeout_seconds": 2400,
            "quota_patterns": list(_DEFAULT_QUOTA_PATTERNS),
        },
    },
}


class AgentConfigError(ValueError):
    """Битый или семантически невалидный agents.yaml."""


@dataclass(frozen=True)
class ExecutorConfig:
    """Одна запись executors.<name> из agents.yaml.

    Поле `home` — внутреннее: раннер подставляет сюда GEODOCS_HOME общей
    базы перед построением исполнителя (agents.yaml его не описывает).
    """

    name: str
    type: str
    command: str
    args: list[str]
    timeout_seconds: int
    quota_patterns: list[str] = field(default_factory=list)
    skills_dirs: list[str] = field(default_factory=list)
    mcp: bool = True
    home: Path | None = None

    def compiled_quota_patterns(self) -> list[re.Pattern[str]]:
        """Компилирует regex квот; битый паттерн — понятная ошибка конфига."""
        compiled: list[re.Pattern[str]] = []
        for pattern in self.quota_patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error as exc:
                raise AgentConfigError(
                    f"исполнитель {self.name!r}: битый regex квоты {pattern!r}: {exc}"
                ) from exc
        return compiled


@dataclass(frozen=True)
class AgentTierConfig:
    """Весь agents.yaml с применёнными defaults.*."""

    chain: list[str]
    executors: dict[str, ExecutorConfig]
    workers: int = 1
    retry_attempts: int = 2
    retry_pause_seconds: int = 60
    run_after_sync: bool = False


def default_config() -> AgentTierConfig:
    """Встроенные дефолты, идентичные agents.yaml по умолчанию."""
    return _parse_config(_BUILTIN_DEFAULTS, source="<встроенные дефолты>")


def config_path(home: Path) -> Path:
    """Путь к agents.yaml: env GEODOCS_AGENTS_CONFIG или <home>/agents.yaml."""
    override = os.environ.get(_ENV_CONFIG)
    if override:
        return Path(override).expanduser()
    return Path(home) / _CONFIG_FILENAME


def load_config(home: Path) -> AgentTierConfig:
    """Читает agents.yaml; отсутствующий файл — встроенные дефолты.

    Битый YAML и семантические ошибки (неизвестный type, дыры в chain и т.п.)
    поднимают AgentConfigError с указанием пути к файлу.
    """
    path = config_path(home)
    if not path.is_file():
        return default_config()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"не удалось разобрать {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AgentConfigError(
            f"{path}: корень должен быть mapping, а не {type(raw).__name__}"
        )
    try:
        return _parse_config(raw, source=str(path))
    except AgentConfigError as exc:
        raise AgentConfigError(f"{path}: {exc}") from exc


def _as_int(value: Any, *, field_name: str, source: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentConfigError(
            f"{source}: поле {field_name!r} должно быть целым числом, а не {value!r}"
        )
    if value < minimum:
        raise AgentConfigError(
            f"{source}: поле {field_name!r} должно быть ≥ {minimum}, а не {value}"
        )
    return value


def _parse_config(raw: dict[str, Any], *, source: str) -> AgentTierConfig:
    # ленивый импорт: executors импортирует этот модуль на верхнем уровне
    from .executors import executor_type_names

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise AgentConfigError(f"{source}: секция 'defaults' должна быть mapping")

    workers = _as_int(
        defaults.get("workers", 1),
        field_name="defaults.workers",
        source=source,
        minimum=1,
    )
    retry_attempts = _as_int(
        defaults.get("retry_attempts", 2),
        field_name="defaults.retry_attempts",
        source=source,
        minimum=1,
    )
    retry_pause = _as_int(
        defaults.get("retry_pause_seconds", 60),
        field_name="defaults.retry_pause_seconds",
        source=source,
    )
    run_after_sync = raw.get("run_after_sync", False)
    if not isinstance(run_after_sync, bool):
        raise AgentConfigError(
            f"{source}: поле 'run_after_sync' должно быть true/false"
        )

    executors_raw = raw.get("executors") or {}
    if not isinstance(executors_raw, dict) or not executors_raw:
        raise AgentConfigError(
            f"{source}: секция 'executors' должна быть непустым mapping"
        )

    executors: dict[str, ExecutorConfig] = {}
    for name, entry in executors_raw.items():
        label = f"исполнитель {name!r}"
        if not isinstance(entry, dict):
            raise AgentConfigError(f"{source}: {label} должен быть mapping")
        entry_type = entry.get("type", name)
        if not isinstance(entry_type, str):
            raise AgentConfigError(
                f"{source}: {label}: поле 'type' должно быть строкой"
            )
        known_types = executor_type_names()
        if entry_type not in known_types:
            known = ", ".join(known_types) or "нет зарегистрированных"
            raise AgentConfigError(
                f"{source}: {label}: неизвестный type {entry_type!r}"
                f" (зарегистрированные: {known})"
            )
        command = entry.get("command", entry_type)
        if not isinstance(command, str) or not command:
            raise AgentConfigError(
                f"{source}: {label}: поле 'command' должно быть непустой строкой"
            )
        args = entry.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AgentConfigError(
                f"{source}: {label}: поле 'args' должно быть списком строк"
            )
        timeout = _as_int(
            entry.get("timeout_seconds", 900),
            field_name=f"{label}.timeout_seconds",
            source=source,
            minimum=1,
        )
        patterns = entry.get("quota_patterns", list(_DEFAULT_QUOTA_PATTERNS))
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) for p in patterns
        ):
            raise AgentConfigError(
                f"{source}: {label}: поле 'quota_patterns' должно быть списком строк"
            )
        skills_dirs = entry.get("skills_dirs", [])
        if not isinstance(skills_dirs, list) or not all(
            isinstance(item, str) for item in skills_dirs
        ):
            raise AgentConfigError(
                f"{source}: {label}: поле 'skills_dirs' должно быть списком строк"
            )
        mcp_raw = entry.get("mcp", True)
        mcp_enabled = True
        if isinstance(mcp_raw, dict):
            mcp_enabled = bool(mcp_raw.get("enabled", True))
        elif isinstance(mcp_raw, bool):
            mcp_enabled = mcp_raw
        else:
            raise AgentConfigError(
                f"{source}: {label}: поле 'mcp' должно быть bool или {{enabled: bool}}"
            )
        cfg = ExecutorConfig(
            name=name,
            type=entry_type,
            command=command,
            args=list(args),
            timeout_seconds=timeout,
            quota_patterns=list(patterns),
            skills_dirs=list(skills_dirs),
            mcp=mcp_enabled,
        )
        cfg.compiled_quota_patterns()  # валидация regex до первого запуска
        executors[name] = cfg

    chain_raw = raw.get("chain")
    if (
        not isinstance(chain_raw, list)
        or not chain_raw
        or not all(isinstance(item, str) for item in chain_raw)
    ):
        raise AgentConfigError(
            f"{source}: поле 'chain' должно быть непустым списком имён исполнителей"
        )
    chain: list[str] = []
    for item in chain_raw:
        if item not in executors:
            raise AgentConfigError(
                f"{source}: в chain указан {item!r}, но executors.{item} не описан"
            )
        if item not in chain:
            chain.append(item)
    return AgentTierConfig(
        chain=chain,
        executors=executors,
        workers=workers,
        retry_attempts=retry_attempts,
        retry_pause_seconds=retry_pause,
        run_after_sync=run_after_sync,
    )
