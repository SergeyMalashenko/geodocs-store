"""Извлечение таблицы ВРИ из HTML полного текста документа (docs.cntd.ru).

CNTD отдаёт документ блоками HTML-фрагментов; таблицы ВРИ — ``<table
class="wideTable">`` внутри секции территориальной зоны. Первая строка
``<tr height="1">`` с пустыми ячейками служебная (ширины колонок) и
пропускается; шапка занимает 1–3 физические строки до первой строки
с кодом вида, колонки определяются по ключевым словам («min», «max»,
«процент», «отступ» и т.п.) с учётом colspan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from ._rows import is_code
from .models import ExtractionOutcome, VriItem, VriTable, parse_number

EXTRACTOR_NAME = "vri-html@1"

_ZONE_ANY_RE = re.compile(
    r"^\s*[А-ЯЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-я]?\s*[-–—]?\s+"
    r"(?:специализированная\s+)?зона\b",
    re.IGNORECASE,
)
_HEADER_NAME_RE = re.compile(
    r"^\s*[А-ЯЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-я]?\s*[-–—]?\s+(?P<name>зона\b.*)$",
    re.IGNORECASE,
)


@dataclass
class _Element:
    """Топ-level элемент документа: заголовок/абзац текста или таблица."""

    kind: str  # "heading" | "paragraph" | "table"
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    table_class: str = ""


class _DocHTMLParser(HTMLParser):
    """Скелетный разбор CNTD-HTML: заголовки, абзацы, строки и ячейки таблиц.

    Вложенные таблицы игнорируются; ``<br/>`` и границы ``<p>`` внутри ячейки
    дают пробел при склейке текста; colspan разворачивается пустыми ячейками,
    чтобы позиции колонок шапки совпадали с позициями в строках данных.
    """

    _HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
    _SKIP_TAGS = frozenset({"style", "script"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._table_depth = 0
        self._table_class = ""
        self._table_rows: list[list[str]] = []
        self._row_cells: list[str] = []
        self._cell_parts: list[str] = []
        self._in_row = False
        self._in_cell = False
        self._cell_span = 1
        self._heading_tag: str | None = None
        self._in_paragraph = False
        self._skip_depth = 0
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "table":
            self._flush_text()
            if self._table_depth == 0:
                self._table_class = dict(attrs).get("class") or ""
                self._table_rows = []
            self._table_depth += 1
            return
        if self._table_depth:
            if self._table_depth == 1 and tag == "tr":
                self._in_row = True
                self._row_cells = []
            elif self._table_depth == 1 and self._in_row and tag in {"td", "th"}:
                self._in_cell = True
                self._cell_parts = []
                raw_span = dict(attrs).get("colspan") or "1"
                self._cell_span = int(raw_span) if raw_span.isdigit() else 1
            elif tag in {"br", "p"} and self._in_cell:
                self._cell_parts.append(" ")
            return
        if tag in self._HEADING_TAGS:
            self._flush_text()
            self._heading_tag = tag
            self._buffer = []
            return
        if tag == "p":
            self._flush_text()
            self._in_paragraph = True
            return
        if tag == "br":
            self._buffer.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            if self._table_depth == 0:
                self.elements.append(
                    _Element(kind="table", rows=self._table_rows,
                             table_class=self._table_class)
                )
            return
        if self._table_depth:
            if self._table_depth == 1 and tag in {"td", "th"} and self._in_cell:
                self._row_cells.append(" ".join("".join(self._cell_parts).split()))
                # colspan разворачивается пустыми ячейками: позиции колонок
                # шапки совпадают с позициями в строках данных
                self._row_cells.extend([""] * (self._cell_span - 1))
                self._in_cell = False
            elif self._table_depth == 1 and tag == "tr" and self._in_row:
                self._table_rows.append(self._row_cells)
                self._in_row = False
            elif self._in_cell and tag == "p":
                self._cell_parts.append(" ")
            return
        if tag in self._HEADING_TAGS and self._heading_tag is not None:
            text = " ".join("".join(self._buffer).split())
            if text:
                self.elements.append(_Element(kind="heading", text=text))
            self._heading_tag = None
            self._buffer = []
            return
        if tag == "p" and self._in_paragraph:
            self._flush_text()
            self._in_paragraph = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_cell:
            self._cell_parts.append(data)
        elif self._heading_tag is not None or self._in_paragraph:
            self._buffer.append(data)

    def close(self) -> None:
        super().close()
        self._flush_text()

    def _flush_text(self) -> None:
        text = " ".join("".join(self._buffer).split())
        if text:
            self.elements.append(_Element(kind="paragraph", text=text))
        self._buffer = []


def _zone_header_re(zone_code: str) -> re.Pattern[str]:
    """Заголовок секции запрошенной зоны: «Ж-2 - зона…»/«СХ-2 зона…»."""
    return re.compile(
        rf"^{re.escape(zone_code)}(?![\w.])\s*[-–—]?\s+зона\b",
        re.IGNORECASE,
    )


def _classify_header_cell(text: str) -> str | None:
    """Ключ колонки по ячейке шапки: row/name/code/area_min/area_max/percent/margin."""
    folded = " ".join(text.split()).casefold()
    if not folded:
        return None
    if "п/п" in folded or folded == "№":
        return "row"
    if "наименование" in folded:
        return "name"
    if "код" in folded or "числовое обозначение" in folded or is_code(text):
        return "code"
    tokens = folded.split()
    if "min" in tokens:
        return "area_min"
    if "max" in tokens:
        return "area_max"
    if "процент" in folded or "%" in folded:
        return "percent"
    if "отступ" in folded:
        return "margin"
    return None


def _column_mapping(header_rows: list[list[str]]) -> dict[str, int]:
    """Позиции колонок по шапке; первое ключевое слово на позиции выигрывает."""
    mapping: dict[str, int] = {}
    for row in header_rows:
        for index, cell in enumerate(row):
            key = _classify_header_cell(cell)
            if key is not None and key not in mapping:
                mapping[key] = index
    return mapping


_CODE_EXTRACT_RE = re.compile(r"^\s*(\d{1,2}\.\d[\d.]*\*{0,4})")


def _clean_code(value: str) -> str | None:
    """Код вида из ячейки: «2.1 <1>» → «2.1» (сноски отбрасываются)."""
    stripped = value.strip()
    if is_code(stripped):
        return stripped
    match = _CODE_EXTRACT_RE.match(stripped)
    if match is not None and is_code(match.group(1)):
        return match.group(1)
    return None


def _parse_table(rows: list[list[str]]) -> tuple[list[VriItem], int]:
    """Строки таблицы: служебные пустые пропуск, шапка до первой строки с кодом."""
    header_rows: list[list[str]] = []
    data_rows: list[list[str]] = []
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue  # служебная строка ширин колонок (<tr height="1">)
        if not data_rows and not any(_clean_code(cell) for cell in row):
            header_rows.append(row)
            continue
        data_rows.append(row)
    mapping = _column_mapping(header_rows)

    def cell(row: list[str], key: str) -> str:
        index = mapping.get(key)
        if index is None or index >= len(row):
            return ""
        return row[index].strip()

    items: list[VriItem] = []
    rows_total = 0
    for row in data_rows:
        code_index = next(
            (i for i, value in enumerate(row) if _clean_code(value)), None
        )
        code = _clean_code(row[code_index]) if code_index is not None else None
        if code is None:
            continue
        rows_total += 1
        area_min = cell(row, "area_min")
        area_max = cell(row, "area_max")
        items.append(
            VriItem(
                row=cell(row, "row"),
                code=code,
                name=cell(row, "name") or None,
                area_min=parse_number(area_min) if area_min else None,
                area_max=parse_number(area_max) if area_max else None,
                building_percentage=cell(row, "percent") or None,
                margin=cell(row, "margin") or None,
                raw=" | ".join(row),
            )
        )
    return items, rows_total


def extract_html(file_path: Path, zone_code: str) -> ExtractionOutcome:
    """Парсит таблицы ВРИ зоны из HTML полного текста документа."""
    try:
        html = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ExtractionOutcome(
            status="error", detail=f"html не прочитан: {exc}", extractor=EXTRACTOR_NAME
        )
    parser = _DocHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - HTMLParser не должен падать на битом HTML
        return ExtractionOutcome(
            status="error", detail=f"html не разобран: {exc}", extractor=EXTRACTOR_NAME
        )

    elements = parser.elements
    zone_re = _zone_header_re(zone_code)
    header_index: int | None = None
    header_text = ""
    for index, element in enumerate(elements):
        if element.kind == "table":
            continue
        if zone_re.match(element.text):
            header_index = index
            header_text = element.text
            break
    if header_index is None:
        return ExtractionOutcome(
            status="no_section",
            detail=f"заголовок зоны {zone_code} не найден",
            extractor=EXTRACTOR_NAME,
        )

    tables: list[_Element] = []
    description_parts: list[str] = []
    for element in elements[header_index + 1 :]:
        if element.kind == "table":
            tables.append(element)
            continue
        if _ZONE_ANY_RE.match(element.text):
            break
        if element.kind == "paragraph" and not tables:
            description_parts.append(element.text)

    wide = [
        table
        for table in tables
        if "widetable" in table.table_class.casefold().split()
    ]
    selected = wide or tables

    items: list[VriItem] = []
    rows_total = 0
    for table in selected:
        table_items, table_rows = _parse_table(table.rows)
        items.extend(table_items)
        rows_total += table_rows

    name_match = _HEADER_NAME_RE.match(header_text)
    description = " ".join(description_parts)[:2000]
    return ExtractionOutcome(
        status="extracted",
        table=VriTable(
            zone_code=zone_code,
            zone_name=name_match.group("name").strip() if name_match else None,
            zone_description=description or None,
            items=items,
            counts={
                "rows_total": rows_total,
                "rows_parsed": len(items),
                "tables": len(selected),
            },
            source_file=str(file_path),
            confidence=1.0,
        ),
        extractor=EXTRACTOR_NAME,
    )
