"""DocumentMatch (§52 ТЗ): соответствие найденного документа запросу.

Детерминированная проверка реквизитов: из текста скачанного файла
извлекаются номер и дата документа и сравниваются с целью acquisition.
LLM не участвует. Исход:

- ``exact``     — номер и дата совпали;
- ``probable``  — номер совпал, дату извлечь не удалось;
- ``ambiguous`` — реквизиты из текста не извлеклись (скан и т.п.);
- ``mismatch``  — извлечён другой номер или другая дата: документ чужой.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel

# «№ 77», «N 944-ВРИ», «номер 77» — номер с необязательным буквенным суффиксом.
_NUMBER_RE = re.compile(r"[№Nn]\s*([0-9]+(?:[-/][0-9A-Za-zА-Яа-я]+)*)")
# «от 15.01.2026», «15.01.2026» — дата в русской записи.
_DATE_RU_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
# ISO-дата в имени файла/тексте.
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

_TEXT_HEAD_CHARS = 20_000  # реквизиты — в начале документа


class DocumentMatch(BaseModel):
    """Итог проверки соответствия документа запрошенной редакции."""

    requested_number: str | None
    requested_date: str | None
    detected_number: str | None
    detected_date: str | None
    source_url: str | None
    status: Literal["exact", "probable", "ambiguous", "mismatch"]


def _norm_number(value: str | None) -> str | None:
    """Нормализация номера: без пробелов, casefold; None/пусто → None."""
    if not value:
        return None
    normalized = "".join(value.split()).casefold().lstrip("0") or "0"
    return normalized


def _norm_date(value: str | None) -> str | None:
    """Дата к ISO; «unknown»/мусор → None."""
    if not value or value == "unknown":
        return None
    match = _DATE_ISO_RE.search(value)
    if match:
        return match.group(0)
    match = _DATE_RU_RE.search(value)
    if match:
        day, month, year = (int(part) for part in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def _detect_requisites(text: str) -> tuple[str | None, str | None]:
    """Первые номер и дата в тексте (сырые строки; нормализация снаружи)."""
    number = None
    match = _NUMBER_RE.search(text)
    if match:
        number = match.group(1)
    detected_date = None
    match = _DATE_ISO_RE.search(text) or _DATE_RU_RE.search(text)
    if match:
        if match.re is _DATE_ISO_RE:
            detected_date = match.group(0)
        else:
            day, month, year = (int(part) for part in match.groups())
            try:
                detected_date = date(year, month, day).isoformat()
            except ValueError:
                detected_date = None
    return number, detected_date


def match_document(
    *,
    number: str | None,
    version_date: str | None,
    text: str | None,
    source_url: str | None,
) -> DocumentMatch:
    """Сравнивает реквизиты цели с извлечёнными из текста документа.

    ``text`` — плоский текст первого файла (None — извлечь не удалось).
    """
    requested_number = _norm_number(number)
    requested_date = _norm_date(version_date)
    detected_number: str | None = None
    detected_date: str | None = None
    if text:
        raw_number, raw_date = _detect_requisites(text[:_TEXT_HEAD_CHARS])
        detected_number = _norm_number(raw_number)
        detected_date = _norm_date(raw_date)

    if detected_number is None and detected_date is None:
        status: Literal["exact", "probable", "ambiguous", "mismatch"] = "ambiguous"
    elif detected_number is not None and detected_number != requested_number or (
        detected_date is not None
        and requested_date is not None
        and detected_date != requested_date
    ):
        status = "mismatch"
    elif detected_number is not None and (
        detected_date is None
        or requested_date is None
        or detected_date == requested_date
    ):
        status = "probable" if detected_date is None else "exact"
    else:  # номер не извлечён, но дата совпала/запрошена — слабое совпадение
        status = "probable" if detected_date == requested_date else "ambiguous"

    return DocumentMatch(
        requested_number=number,
        requested_date=version_date,
        detected_number=detected_number,
        detected_date=detected_date,
        source_url=source_url,
        status=status,
    )
