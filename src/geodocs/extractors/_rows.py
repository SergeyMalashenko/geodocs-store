"""Общая сборка VriItem из упорядоченных текстовых фрагментов строки."""

from __future__ import annotations

import re
from typing import Any

from .models import (
    NOT_APPLICABLE,
    NOT_ESTABLISHED,
    VriItem,
    normalize_keyword,
    parse_number,
)

_CODE_RE = re.compile(r"^\d{1,2}\.\d[\d.]*\*{0,4}$")
_TOKEN_SPLIT_RE = re.compile(r"\s{2,}")

HEADER_KEYWORDS = (
    "предельные размеры",
    "наименование ври",
    "числовое обозначение",
    "максимальный процент",
    "минимальные отступы",
    "архитектурно-",
    "градострои-",
    "основные виды",
    "вспомогательные виды",
    "условно разрешенные",
    "иные показатели",
    "ст. ",
    "правил)",
    "не подлежат",
    "устанавливаются",
    "установлению",
)
HEADER_TOKENS = ("№ п/п", "п/п", "min", "max", "(кв. м)", "(м)")


def is_code(value: str) -> bool:
    """Код вида «2.1*», «3.1.1», «12.0.1», «4.4***»."""
    return bool(_CODE_RE.match(value.strip()))


def is_header_line(value: str) -> bool:
    """Строка шапки/служебная: не кандидат в наименования ВРИ."""
    normalized = normalize_keyword(value)
    if any(keyword in normalized for keyword in HEADER_KEYWORDS):
        return True
    folded = value.strip().casefold()
    return any(folded == token for token in HEADER_TOKENS)


def split_row_tokens(text: str) -> list[str]:
    """Разбивает хвост строки таблицы на колонки по разрывам из 2+ пробелов."""
    return [token for token in _TOKEN_SPLIT_RE.split(text) if token.strip()]


def _classify(token: str) -> tuple[str, Any]:
    normalized = normalize_keyword(token)
    if not normalized or normalized in {"—", "-", "–"}:
        return "empty", None
    if NOT_ESTABLISHED in normalized:
        return "na", None
    if NOT_APPLICABLE in normalized:
        return "na", None
    if "%" in token or "эт." in normalized or "этаж" in normalized:
        return "percent", " ".join(token.split())
    compact = token.replace("\u00a0", " ")
    if re.fullmatch(r"\d[\d ]*(?:[.,]\d+)?\*{0,4}", compact.strip()):
        return "number", parse_number(compact)
    if re.fullmatch(r"\d[\d ]*\([^)]*\)", compact.strip()):
        return "margin", " ".join(compact.split())
    return "text", " ".join(token.split())


def assemble_item(row: str, code: str, name: str | None, tokens: list[str], raw: str) -> VriItem:
    """Собирает VriItem из колонок после кода (min, max, %, отступы, облик)."""
    columns: dict[str, Any] = {
        "area_min": None,
        "area_max": None,
        "building_percentage": None,
        "margin": None,
    }
    oblik: list[str] = []
    order = ("area_min", "area_max", "building_percentage", "margin")
    cursor = 0
    kinds = [_classify(token) for token in tokens]
    # «Не подлежат установлению | 3» — маркер min/max и одиночный отступ.
    if (
        len(kinds) == 2
        and kinds[0][0] == "na"
        and kinds[1][0] in {"number", "margin"}
    ):
        columns["margin"] = kinds[1][1]
        kinds = []
    for kind, value in kinds:
        if kind == "number":
            if cursor < len(order):
                columns[order[cursor]] = value
                cursor += 1
        elif kind == "margin":
            columns["margin"] = value
            cursor = max(cursor, 4)
        elif kind == "percent":
            columns["building_percentage"] = value
            cursor = max(cursor, 3)
        elif kind == "na":
            cursor = min(cursor + 1, 4)
        elif kind == "text":
            oblik.append(value)
    return VriItem(
        row=row,
        code=code,
        name=" ".join(name.split()) if name else None,
        area_min=columns["area_min"],
        area_max=columns["area_max"],
        building_percentage=columns["building_percentage"],
        margin=columns["margin"],
        raw=raw,
    )
