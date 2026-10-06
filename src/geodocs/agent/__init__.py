"""Агентный ярус документного поиска: внешние агенты добирают не найденное статикой."""

from .config import (
    AgentConfigError,
    AgentTierConfig,
    ExecutorConfig,
    default_config,
    load_config,
)
from .executors import (
    ExecutionResult,
    ExecutionTimeout,
    Executor,
    ExecutorError,
    HermesExecutor,
    KimiExecutor,
    QuotaExceeded,
    build_executor,
)
from .gate import FileVerdict, gate_pass, verify_files
from .prompt import PROMPT_TEMPLATE, build_prompt, doc_type_ru
from .runner import (
    MANIFEST_MARKER,
    TaskResult,
    inbox_dir,
    parse_manifest,
    recover_inbox,
    run_agent_tier,
    run_pending,
    run_task,
)
from .tasks import AgentTask, list_pending_tasks

__all__ = [
    "MANIFEST_MARKER",
    "PROMPT_TEMPLATE",
    "AgentConfigError",
    "AgentTask",
    "AgentTierConfig",
    "ExecutionResult",
    "ExecutionTimeout",
    "Executor",
    "ExecutorConfig",
    "ExecutorError",
    "FileVerdict",
    "HermesExecutor",
    "KimiExecutor",
    "QuotaExceeded",
    "TaskResult",
    "build_executor",
    "build_prompt",
    "default_config",
    "doc_type_ru",
    "gate_pass",
    "inbox_dir",
    "list_pending_tasks",
    "load_config",
    "parse_manifest",
    "recover_inbox",
    "run_agent_tier",
    "run_pending",
    "run_task",
    "verify_files",
]
