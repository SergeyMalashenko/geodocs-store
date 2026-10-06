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
from .query import (
    RESULT_MARKER,
    Evidence,
    QueryResult,
    QueryStatus,
    ResolvedDocument,
    build_query_prompt,
    parse_result,
    query_documents,
)
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
    "RESULT_MARKER",
    "AgentConfigError",
    "AgentTask",
    "AgentTierConfig",
    "AskResult",
    "Evidence",
    "ExecutionResult",
    "ExecutionTimeout",
    "Executor",
    "ExecutorConfig",
    "ExecutorError",
    "FileVerdict",
    "HermesExecutor",
    "QueryResult",
    "QueryStatus",
    "QuotaExceeded",
    "ResolvedDocument",
    "TaskResult",
    "ask_document",
    "build_ask_prompt",
    "build_executor",
    "build_prompt",
    "build_query_prompt",
    "default_config",
    "doc_type_ru",
    "gate_pass",
    "inbox_dir",
    "list_pending_tasks",
    "load_config",
    "parse_manifest",
    "parse_result",
    "query_documents",
    "recover_inbox",
    "register_executor_type",
    "run_agent_tier",
    "run_pending",
    "run_task",
    "verify_files",
]
