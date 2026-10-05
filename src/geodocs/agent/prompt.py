"""Промпт для внешнего агента (KIMI CLI, headless): поиск и скачивание документа."""

from __future__ import annotations

from pathlib import Path

from .tasks import AgentTask

PROMPT_TEMPLATE = """\
Ты — документальный агент гибридного поиска TerraLogicX. Задача: найти в открытом доступе и скачать конкретный муниципальный правовой документ Московской области. Документ гарантированно публичен.
ЦЕЛЬ: {slug} | тип: {doc_type_ru} | номер: № {number} от {date} | муниципалитет: {municipality} | издатель: {issuer} | название: {title}
Ищи именно ЭТУ редакцию/изменения.
СТРАТЕГИЯ (по приоритету): 1) официальный портал муниципалитета (разделы «Правовые акты», «Градостроительство», «Территориальное планирование») по номеру документа; 2) pravo.gov.ru; 3) docs.cntd.ru — только бесплатное; 4) mosreg.ru и порталы министерств области. Генпланы публикуются пакетом (текст + карты) — качай все приложения одного решения.
СКАЧИВАНИЕ: каталог {inbox_dir} (mkdir -p). Имена: <дата-YYYY-MM-DD>_<короткое-название>.pdf/.docx, суффиксы _1, _2 для нескольких. Скачивай curl -L (URL в кавычках), проверяй file/размер. Вежливость: ≤1 запрос/2с к сайту, ≤30 шагов инструментов.
ЖЁСТКИЕ ПРАВИЛА: «найдено» = файл лежит в каталоге и валиден. Не выдумывай URL. Если только платный источник — not_found. HTML-страница без файла — сохрани .html и пометь partial.
ФИНАЛ — последней строкой строго: === MANIFEST === {{"status": "found|partial|not_found", "files": ["..."], "source_url": "...", "page_title": "...", "notes": "...", "steps_used": N}}
"""

_DOC_TYPE_RU = {
    "pzz": "Правила землепользования и застройки (изменения)",
    "general_plan": "Генеральный план (изменения)",
    "zouit_regime": "Постановление о режимах использования земель (ЗОУИТ)",
}


def doc_type_ru(doc_type: str) -> str:
    """Человекочитаемое название типа документа для промпта (неизвестное — как есть)."""
    return _DOC_TYPE_RU.get(doc_type, doc_type)


def build_prompt(task: AgentTask, inbox_dir: str | Path) -> str:
    """Заполняет шаблон промпта полями задачи и каталогом для скачанных файлов."""
    return PROMPT_TEMPLATE.format(
        slug=task.slug,
        doc_type_ru=doc_type_ru(task.doc_type),
        number=task.number,
        date=task.version_date,
        municipality=task.municipality,
        issuer=task.issuer or "не указан",
        title=task.title or "не указано",
        inbox_dir=str(inbox_dir),
    )
