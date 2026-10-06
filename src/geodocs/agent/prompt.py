"""Промпт для внешнего агента (Kimi CLI, headless): поиск и скачивание документа."""

from __future__ import annotations

from pathlib import Path

from .tasks import AgentTask

PROMPT_TEMPLATE = """\
Ты — документальный агент гибридного поиска TerraLogicX с инструментами. Задача: найти в открытом доступе и скачать конкретный муниципальный правовой документ Московской области. Документ гарантированно публичен.
ЦЕЛЬ: {slug} | тип: {doc_type_ru} | номер: № {number} от {date} | муниципалитет: {municipality} | издатель: {issuer} | название: {title}
Ищи именно ЭТУ редакцию/изменения.
ПОРЯДОК ДЕЙСТВИЙ (строго по шагам, веб-поиск — только последнее звено):
1) Вызови инструмент check_local_store(doc_type, number, version_date): если версия уже скачана — ничего не ищи, сразу печатай MANIFEST со status="not_found" и notes="уже в локальной базе".
2) Вызови search_document(municipality, doc_type, number, version_date, title) — параллельный поиск по порталам. Изучи кандидатов: номер и дата должны совпадать с ЦЕЛЬЮ. Подсказки по порталам — в подключённых скилах (meganorm-search, cntd-search и др.).
3) Для подходящего кандидата вызови download_document(url, portal=<тег кандидата>): файл попадёт в inbox автоматически. Муниципальные сайты без API: fetch_page(url) отдаст текст страницы и ссылки на файлы — скачай нужные через download_document.
4) СВОБОДНЫЙ ВЕБ-ПОИСК — только если шаги 1-3 не дали файла: ищи официальный портал муниципалитета (разделы «Документы территориального планирования», «Нормативные акты»), pravo.gov.ru, mosreg.ru. Как ходить по муниципальным сайтам — в скиле municipal-navigation. Реквизиты из названия извлекай по скилу document-requisites.
СКАЧИВАНИЕ: каталог {inbox_dir} (mkdir -p). Имена файлов инструменты выбирают сами; при скачивании вручную: <дата-YYYY-MM-DD>_<короткое-название>.pdf/.docx, суффиксы _1, _2 для нескольких. Вежливость: ≤1 запрос/2с к сайту, ≤30 шагов инструментов.
ЖЁСТКИЕ ПРАВИЛА: «найдено» = файл лежит в каталоге и валиден. Не выдумывай URL. HTML-страница — НЕ документ (гейт её отклонит): если портал отдал HTML вместо PDF/DOCX — ищи прямую ссылку на файл или другой источник. Если только платный источник — not_found.
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
