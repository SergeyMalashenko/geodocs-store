"""Агентный ярус документного поиска: внешние агенты добирают не найденное статикой."""

from .ask import AskResult, ask_document, build_ask_prompt
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
    QuotaExceeded,
    build_executor,
    register_executor_type,
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
    "AskResult",
    "ExecutionResult",
    "ExecutionTimeout",
    "Executor",
    "ExecutorConfig",
    "ExecutorError",
    "FileVerdict",
    "HermesExecutor",
    "QuotaExceeded",
    "TaskResult",
    "ask_document",
    "build_ask_prompt",
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
    "register_executor_type",
    "run_agent_tier",
    "run_pending",
    "run_task",
    "verify_files",
]
