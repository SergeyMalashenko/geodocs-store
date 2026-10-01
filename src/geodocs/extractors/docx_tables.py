"""Извлечение таблицы ВРИ из DOCX (stdlib zipfile + regex, без зависимостей)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from ._rows import assemble_item, is_code
from .models import ExtractionOutcome, VriItem, VriTable

EXTRACTOR_NAME = "vri-docx@1"

_PARAGRAPH_RE = re.compile(r"<w:p[ >].*?</w:p>", re.DOTALL)
_ROW_RE = re.compile(r"<w:tr[ >].*?</w:tr>", re.DOTALL)
_CELL_RE = re.compile(r"<w:tc[ >].*?</w:tc>", re.DOTALL)
_TEXT_RE = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")
_SECTION_START_RE = re.compile(
    r"^\s*(?P<code>[А-ЯA-ZЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-яA-Za-z]?)"
    r"\s*(?:[-–—]\s*)?(?P<name>ЗОНА\b.*)$",
    re.IGNORECASE,
)
_DESCRIPTION_STOP = ("основные виды разрешенного использования",)


def _cell_text(cell_xml: str) -> str:
    return "".join(_TEXT_RE.findall(cell_xml))


def _paragraph_text(paragraph_xml: str) -> str:
    return "".join(_TEXT_RE.findall(paragraph_xml))


def _zone_header(paragraphs: list[str], zone_code: str) -> tuple[str, str] | None:
    """Ищет «Ж-2 - ЗОНА …» среди первых абзацев; код запрошенной зоны совпадать не обязан."""
    expected = zone_code.casefold()
    for paragraph in paragraphs:
        match = _SECTION_START_RE.match(paragraph.strip())
        if match is None:
            continue
        if match.group("code").casefold() == expected:
            return match.group("code"), match.group("name").strip()
    return None


def _zone_description(paragraphs: list[str], zone_code: str) -> str | None:
    expected = zone_code.casefold()
    collecting = False
    parts: list[str] = []
    for paragraph in paragraphs:
        folded = " ".join(paragraph.split()).casefold()
        if not collecting:
            if folded.startswith(expected):
                collecting = True
            continue
        if any(folded.startswith(stop) for stop in _DESCRIPTION_STOP):
            break
        if paragraph.strip():
            parts.append(" ".join(paragraph.split()))
    if not parts:
        return None
    return " ".join(parts)[:2000]


def extract_docx(file_path: Path, zone_code: str) -> ExtractionOutcome:
    """Парсит таблицы ВРИ из DOCX-регламента территориальной зоны."""
    try:
        with zipfile.ZipFile(file_path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        return ExtractionOutcome(
            status="error", detail=f"docx не прочитан: {exc}", extractor=EXTRACTOR_NAME
        )

    paragraphs = [_paragraph_text(p) for p in _PARAGRAPH_RE.findall(xml)]
    paragraphs = [p for p in paragraphs if p.strip()]
    header = _zone_header(paragraphs, zone_code)
    if header is None:
        return ExtractionOutcome(
            status="no_section",
            detail=f"заголовок зоны {zone_code} не найден",
            extractor=EXTRACTOR_NAME,
        )
    code, zone_name = header
    description = _zone_description(paragraphs, zone_code)

    items: list[VriItem] = []
    rows_total = 0
    for row_xml in _ROW_RE.findall(xml):
        cells = [_cell_text(c) for c in _CELL_RE.findall(row_xml)]
        cells = [" ".join(c.split()) for c in cells]
        code_index = next(
            (i for i, cell in enumerate(cells) if is_code(cell)), None
        )
        if code_index is None or code_index == 0:
            continue
        rows_total += 1
        row_number = cells[0] if cells[0].isdigit() else ""
        name = cells[1] if code_index > 1 else None
        item = assemble_item(
            row=row_number,
            code=cells[code_index],
            name=name,
            tokens=cells[code_index + 1 :],
            raw=" | ".join(cells),
        )
        items.append(item)

    return ExtractionOutcome(
        status="extracted",
        table=VriTable(
            zone_code=code,
            zone_name=zone_name or None,
            zone_description=description,
            items=items,
            counts={"rows_total": rows_total, "rows_parsed": len(items)},
            source_file=str(file_path),
            confidence=1.0,
        ),
        extractor=EXTRACTOR_NAME,
    )
