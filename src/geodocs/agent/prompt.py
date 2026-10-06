"""Промпт для внешнего агента (Hermes Agent, headless): поиск и скачивание документа."""

from __future__ import annotations

from pathlib import Path

from .tasks import AgentTask

PROMPT_TEMPLATE = """\
Ты — документальный агент гибридного поиска TerraLogicX с инструментами. Задача: найти в открытом доступе и скачать конкретный муниципальный правовой документ Московской области. Документ гарантированно публичен.
ЦЕЛЬ: {slug} | тип: {doc_type_ru} | номер: № {number} от {date} | муниципалитет: {municipality} | издатель: {issuer} | название: {title}
Ищи именно ЭТУ редакцию/изменения.
ПОРЯДОК ДЕЙСТВИЙ (строго по шагам, веб-поиск — только последнее звено):
1) Вызови find_document(municipality, doc_type, number, version_date, title): если local=true и у версии fetch_status="downloaded" — ничего не ищи, сразу печатай MANIFEST со status="not_found" и notes="уже в локальной базе". Иначе изучи candidates: номер и дата должны совпадать с ЦЕЛЬЮ. Подсказки по порталам — в подключённых скилах (meganorm-search, cntd-search и др.).
2) Для подходящего кандидата вызови import_document(url, portal=<тег кандидата>, doc_hint={{"municipality": "{municipality}", "doc_type": "{doc_type}", "number": "{number}", "version_date": "{date}", "title": "{title}"}}): инструмент сам скачает файлы (HTML-страницы обрабатывает тоже — дотянет с них PDF/DOCX), проверит и зарегистрирует в базе. Успех = version_id в ответе.
3) СВОБОДНЫЙ ВЕБ-ПОИСК — только если шаги 1-2 не дали version_id: используй встроенные веб-инструменты агента (поиск и чтение страниц, Firecrawl); ищи официальный портал муниципалитета (разделы «Документы территориального планирования», «Нормативные акты»), pravo.gov.ru, mosreg.ru. Как ходить по муниципальным сайтам — в скиле municipal-navigation. Реквизиты из названия извлекай по скилу document-requisites. Найденный файл импортируй через import_document с тем же doc_hint.
СКАЧИВАНИЕ: файлы, добытые вручную (вне import_document), клади в каталог {inbox_dir} (mkdir -p): <дата-YYYY-MM-DD>_<короткое-название>.pdf/.docx, суффиксы _1, _2 для нескольких. Вежливость: ≤1 запрос/2с к сайту, ≤30 шагов инструментов.
ЖЁСТКИЕ ПРАВИЛА: «найдено» = import_document вернул version_id ИЛИ валидный файл лежит в каталоге. Не выдумывай URL. HTML-страница — НЕ документ (гейт её отклонит). Если только платный источник — not_found.
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
        doc_type=task.doc_type,
        number=task.number,
        date=task.version_date,
        municipality=task.municipality,
        issuer=task.issuer or "не указан",
        title=task.title or "не указано",
        inbox_dir=str(inbox_dir),
    )
