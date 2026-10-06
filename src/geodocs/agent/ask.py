"""Единый метод извлечения сведений из документов локальной базы: Q&A.

`ask_document(query)` принимает свободный текстовый запрос («Верни ВРИ для
документа X», «Какой процент застройки в зоне Ж-2 Коломны?») и отвечает на
него LLM-агентом поверх read-only инструментов локальной базы
(find_documents → get_extractions / read_document_text). Статические
экстракторы (VriExtractor) остаются дешёвым первым звеном: агент проверяет
готовые extractions до чтения полного текста.
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
1) Выдели из вопроса муниципалитет (город/округ) и/или номер документа и вызови find_documents с ЭТИМ значением. НЕ ищи по коду зоны (СХ-2, Ж-1 и т.п.) и виду использования: их нет в номере/названии документа — они проверяются на шагах 2-3. Если найдено несколько версий — выбери подходящую по дате/номеру из вопроса, при сомнении бери самую свежую.
2) get_extractions(version_id) — проверь готовые структурированные фрагменты (таблицы ВРИ по зонам): если ответ уже там — используй их (ищи нужную зону по zone_code).
3) Если фрагментов нет: document_files(version_id) → read_document_text(version_id, file_index) — читай файлы; длинные документы дочитывай порциями (max_chars), пока не найдёшь ответ.
ЖЁСТКИЕ ПРАВИЛА: отвечай только фактами из базы; «в локальной базе нет» пиши только если find_documents не нашёл документ ИЛИ прочитанные фрагменты/текст не содержат ответа. Не выдумывай номера и нормы. В конце укажи источник: муниципалитет, тип и номер документа, дата версии, название файла.
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
