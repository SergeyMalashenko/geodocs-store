"""Единый метод извлечения сведений из документов локальной базы: Q&A.

`ask_document(query)` принимает свободный текстовый запрос («Верни ВРИ для
документа X», «Какой процент застройки в зоне Ж-2 Коломны?») и отвечает на
него LLM-агентом поверх инструментов локальной базы (find_document →
read_document). Для запросов к уже известным документам с типизированным
результатом см. agent/query.py (query_documents) — ask_document остаётся
тонким сахаром для discovery-Q&A без перечня документов.
"""

from __future__ import annotations

import dataclasses
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import AgentTierConfig, load_config
from .executors import Executor, build_executor

ASK_PROMPT_TEMPLATE = """\
Ты — аналитик локальной базы муниципальных правовых документов geodocs (ПЗЗ, генпланы, ЗОУИТ Московской области). Отвечаешь на вопросы ТОЛЬКО по документам базы.
ВОПРОС: {query}
ПОРЯДОК ДЕЙСТВИЙ:
1) Выдели из вопроса муниципалитет (город/округ) и/или номер документа и вызови find_document с ЭТИМИ значениями (number/version_date/doc_type — только если они есть в вопросе). НЕ ищи по коду зоны (СХ-2, Ж-1 и т.п.) и виду использования: их нет в реквизитах документа — они проверяются на шаге 2. Если найдено несколько версий — выбери подходящую по дате/номеру из вопроса, при сомнении бери самую свежую с fetch_status="downloaded".
2) read_document(version_id, query=<суть вопроса: код зоны, вид использования, норма>) — инструмент сам проверит готовые структурированные фрагменты (таблицы ВРИ по зонам) и найдёт релевантные места в тексте файлов. При необходимости повтори с другим query или вызови read_document(version_id) без query — карточка документа и начало текста.
ЖЁСТКИЕ ПРАВИЛА: отвечай только фактами из базы; «в локальной базе нет» пиши только если find_document не нашёл документ ИЛИ read_document не дал ответа. Не выдумывай номера и нормы. В конце укажи источник: муниципалитет, тип и номер документа, дата версии, название файла.
ФИНАЛ: развёрнутый ответ на вопрос на русском языке, последней строкой — источник.
"""


@dataclass(frozen=True)
class AskResult:
    """Ответ агента на запрос к локальной базе документов."""

    query: str
    answer: str
    executor: str
    returncode: int
    duration_seconds: float


def build_ask_prompt(query: str) -> str:
    """Промт Q&A: вопрос + порядок работы с read-only инструментами."""
    return ASK_PROMPT_TEMPLATE.format(query=query)


def _ask_executor(
    home: Path, executor_name: str, config: AgentTierConfig | None
) -> Executor:
    cfg = config or load_config(home)
    entry = cfg.executors.get(executor_name)
    if entry is None:
        known = ", ".join(cfg.executors) or "пусто"
        raise ValueError(
            f"исполнитель {executor_name!r} не описан в agents.yaml (есть: {known})"
        )
    # home общей базы — в MCP-инструменты (как в runner.run_pending).
    return build_executor(dataclasses.replace(entry, home=home))


def ask_document(
    query: str,
    *,
    home: str | Path | None = None,
    executor_name: str = "hermes",
    config: AgentTierConfig | None = None,
    workdir: Path | None = None,
) -> AskResult:
    """Отвечает на свободный запрос по документам локальной базы.

    home — GEODOCS_HOME общей базы (по умолчанию из env/дефолта DocumentStore).
    Ответ — stdout агента (hermes -z печатает только финальный текст).
    """
    if home is None:
        import os

        home = Path(os.environ.get("GEODOCS_HOME", str(Path.home() / ".geodocs")))
    home = Path(home)
    executor = _ask_executor(home, executor_name, config)
    prompt = build_ask_prompt(query)
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="geodocs-ask-") as tmp:
            result = executor.run(prompt, Path(tmp))
    else:
        result = executor.run(prompt, workdir)
    return AskResult(
        query=query,
        answer=result.stdout.strip(),
        executor=executor.name,
        returncode=result.returncode,
        duration_seconds=result.duration_seconds,
    )
