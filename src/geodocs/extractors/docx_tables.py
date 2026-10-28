"""Извлечение таблицы ВРИ из DOCX (stdlib zipfile + regex, без зависимостей).

Документ обходится в порядке следования блоков (абзацы вне таблиц и таблицы):
заголовок «КОД — ЗОНА …» открывает секцию зоны, строки таблиц берутся только
из секции запрошенной зоны (многозональные файлы ПЗЗ). Наименование ВРИ —
ячейка непосредственно слева от кода: угловая пустая ячейка шапки не смещает
колонки (v2; в v1 наименованием мог стать номер строки, а строки собирались
из всех таблиц документа).
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from ._rows import assemble_item, is_code
from .models import ExtractionOutcome, VriItem, VriTable, parse_number

EXTRACTOR_NAME = "vri-docx@2"

_PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.DOTALL)
_TBL_RE = re.compile(r"<w:tbl[ >].*?</w:tbl>", re.DOTALL)
_ROW_RE = re.compile(r"<w:tr[ >].*?</w:tr>", re.DOTALL)
_CELL_RE = re.compile(r"<w:tc[ >].*?</w:tc>", re.DOTALL)
_TEXT_RE = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")
_SECTION_START_RE = re.compile(
    r"^\s*(?P<code>[А-ЯA-ZЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-яA-Za-z]?(?:-[А-ЯA-ZЁ]{1,2})?)"
    r"\s*(?:[-–—]\s*)?(?P<name>ЗОНА\b.*)$",
    re.IGNORECASE,
)
_DESCRIPTION_STOP = ("основные виды разрешенного использования",)
_NA_MARKERS = ("не подлежат", "не распространяется")
# Числовая ячейка регламента: «3», «400 ()», «30 (15****)», «500 000 (1 500*****)»
_NUMERIC_CELL_RE = re.compile(r"^\d[\d .]*(?:\([^)]*\))?\s*\*{0,5}$")
_LEAD_NUMBER_RE = re.compile(r"^\s*(\d[\d .]*(?:,\d+)?)")


def _cell_text(cell_xml: str) -> str:
    return "".join(_TEXT_RE.findall(cell_xml))


def _paragraph_text(paragraph_xml: str) -> str:
    return "".join(_TEXT_RE.findall(paragraph_xml))


def _zone_description(section_paragraphs: list[str]) -> str | None:
    """Описание зоны из абзацев её секции до маркера таблицы."""
    parts: list[str] = []
    for paragraph in section_paragraphs:
        folded = " ".join(paragraph.split()).casefold()
        if any(folded.startswith(stop) for stop in _DESCRIPTION_STOP):
            break
        parts.append(" ".join(paragraph.split()))
    if not parts:
        return None
    return " ".join(parts)[:2000]


def _lead_number(token: str) -> int | float | None:
    """Ведущее число ячейки: «30 (15****)» → 30, «400 ()» → 400."""
    match = _LEAD_NUMBER_RE.match(token)
    if match is None:
        return None
    return parse_number(match.group(1))


def _is_na(token: str) -> bool:
    folded = token.casefold()
    return any(marker in folded for marker in _NA_MARKERS)


def _assemble_docx_item(
    row: str, code: str, name: str | None, tokens: list[str], raw: str
) -> VriItem:
    """Строка ВРИ из DOCX: колонки после кода позиционно (min, max, %, отступ).

    В DOCX ячейки точные (в отличие от потокового PDF-текста), поэтому
    эвристика assemble_item не нужна; варианты в скобках («30 (15****)»)
    берутся по ведущему числу. Если первая колонка нечисловая — откат
    на эвристику (нестандартная сетка таблицы).
    """
    if not tokens or (
        not _NUMERIC_CELL_RE.match(tokens[0] or "") and not _is_na(tokens[0])
    ):
        return assemble_item(row=row, code=code, name=name, tokens=tokens, raw=raw)

    def _cell(index: int) -> str | None:
        if index >= len(tokens):
            return None
        token = tokens[index]
        return None if _is_na(token) else token

    min_token = _cell(0)
    max_token = _cell(1)
    percent_token = _cell(2)
    margin_token = _cell(3)
    return VriItem(
        row=row,
        code=code,
        name=name,
        area_min=_lead_number(min_token) if min_token is not None else None,
        area_max=_lead_number(max_token) if max_token is not None else None,
        building_percentage=(
            " ".join(percent_token.split())
            if percent_token is not None and "%" in percent_token
            else None
        ),
        margin=_lead_number(margin_token) if margin_token is not None else None,
        raw=raw,
    )


def extract_docx(file_path: Path, zone_code: str) -> ExtractionOutcome:
    """Парсит таблицу ВРИ запрошенной зоны из DOCX-регламента."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        return ExtractionOutcome(
            status="error", detail=f"docx не прочитан: {exc}", extractor=EXTRACTOR_NAME
        )

    table_matches = list(_TBL_RE.finditer(xml))
    table_events = [(m.start(), "tbl", m.group(0)) for m in table_matches]
    table_spans = [(m.start(), m.end()) for m in table_matches]
    paragraph_events = [
        (m.start(), "p", _paragraph_text(m.group(0)))
        for m in _PARAGRAPH_RE.finditer(xml)
        if not any(start <= m.start() < end for start, end in table_spans)
    ]
    events = sorted(paragraph_events + table_events)
    expected = zone_code.casefold()

    header_found = False
    zone_name: str | None = None
    current_zone: str | None = None
    section_paragraphs: list[str] = []
    items: list[VriItem] = []
    rows_total = 0
    for _, kind, payload in events:
        if kind == "p":
            text = payload.strip()
            if not text:
                continue
            match = _SECTION_START_RE.match(text)
            if match is not None:
                current_zone = match.group("code").casefold()
                if current_zone == expected and not header_found:
                    header_found = True
                    zone_name = match.group("name").strip()
                continue
            if current_zone == expected:
                section_paragraphs.append(text)
            continue
        if current_zone != expected:
            continue
        for row_xml in _ROW_RE.findall(payload):
            cells = [_cell_text(c) for c in _CELL_RE.findall(row_xml)]
            cells = [" ".join(c.split()) for c in cells]
            code_index = next(
                (i for i, cell in enumerate(cells) if is_code(cell)), None
            )
            if code_index is None or code_index == 0:
                continue
            rows_total += 1
            row_number = cells[0] if cells[0].isdigit() else ""
            name = cells[code_index - 1] if code_index >= 1 else None
            if name is not None and name.isdigit():
                name = None  # колонки сдвинуты: слева от кода номер строки
            item = _assemble_docx_item(
                row=row_number,
                code=cells[code_index],
                name=name,
                tokens=cells[code_index + 1 :],
                raw=" | ".join(cells),
            )
            items.append(item)

    if not header_found:
        return ExtractionOutcome(
            status="no_section",
            detail=f"заголовок зоны {zone_code} не найден",
            extractor=EXTRACTOR_NAME,
        )
    return ExtractionOutcome(
        status="extracted",
        table=VriTable(
            zone_code=zone_code,
            zone_name=zone_name,
            zone_description=_zone_description(section_paragraphs),
            items=items,
            counts={"rows_total": rows_total, "rows_parsed": len(items)},
            source_file=str(file_path),
            confidence=1.0,
        ),
        extractor=EXTRACTOR_NAME,
    )
