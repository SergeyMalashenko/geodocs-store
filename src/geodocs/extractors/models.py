"""Модели итогов извлечения таблиц ВРИ."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class VriItem(BaseModel):
    """Одна строка таблицы видов разрешенного использования."""

    row: str
    code: str
    name: str | None = None
    area_min: int | float | str | None = None
    area_max: int | float | str | None = None
    building_percentage: str | None = None
    margin: int | float | str | None = None
    raw: str


class VriTable(BaseModel):
    """Таблица ВРИ зоны, извлечённая из файла."""

    zone_code: str
    zone_name: str | None = None
    zone_description: str | None = None
    items: list[VriItem] = []
    counts: dict[str, int] = {}
    source_file: str
    confidence: float = 1.0


ExtractionStatus = Literal["extracted", "no_section", "scan_pdf", "no_pdftotext", "error"]


class ExtractionOutcome(BaseModel):
    """Итог попытки извлечь таблицу ВРИ из одного файла."""

    status: ExtractionStatus
    detail: str | None = None
    table: VriTable | None = None
    extractor: str | None = None


# Значения, которые нормализуются в «не установлено» при парсинге строк.
NOT_ESTABLISHED = "не подлежат установлению"
NOT_APPLICABLE = "не распространяется"

# Латинские буквы, подменяющие кириллицу в извлечённом из PDF тексте
# (артефакты кодировки шрифтов, напр. «подлежaт» с латинской «a»).
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "a": "а",
        "e": "е",
        "o": "о",
        "p": "р",
        "c": "с",
        "x": "х",
        "y": "у",
        "B": "В",
        "H": "Н",
        "K": "К",
        "M": "М",
        "T": "Т",
    }
)


def normalize_keyword(value: str) -> str:
    """Ключевые слова для сравнений: латинские двойники → кириллица, casefold."""
    return " ".join(value.split()).casefold().translate(_LATIN_TO_CYRILLIC)


def parse_number(value: str) -> int | float | str:
    """«500» → 500, «5 000 000» → 5000000, «200****» → 200, прочее — как строка."""
    cleaned = " ".join(value.replace("\u00a0", " ").split())
    digits = cleaned.rstrip("*").strip()
    if not digits:
        return cleaned
    compact = digits.replace(" ", "")
    if compact.isdigit():
        return int(compact)
    if "," in compact and re_fullmatch_float(compact):
        return float(compact.replace(",", "."))
    return cleaned


def re_fullmatch_float(value: str) -> bool:
    parts = value.split(",")
    return (
        len(parts) == 2
        and parts[0].isdigit()
        and parts[1].isdigit()
    )
