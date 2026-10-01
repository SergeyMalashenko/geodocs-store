"""Детектирование и исправление сбитой кириллической кодировки (mojibake).

Некоторые ПЗЗ публикуются в PDF с картой символов, уехавшей в диапазон
U+0230–U+02AF: «Коммунальное обслуживание» превращается в «ȿɨɦɦʋɧɚɥ…».
Демаппинг: chr(ord(c) + 0x1D6) для 0x230 ≤ ord ≤ 0x2AF, плюс точечные
замены ©→«, ª→», ѡ→№ и управляющие символы-заменители (цифры, точка,
запятая, кавычки), встречающиеся в таких файлах.
"""

from __future__ import annotations

import re

_MOJIBAKE_RE = re.compile(r"[Ȁ-ʯѡ±]")
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")

# Управляющие символы-заменители (встречаются в битых кодировках):
# \x03 — пробел, \x05 — прямая кавычка, \x08 — знак процента,
# \x0f и \x1e — запятая, \x11 — точка, \x12 — №, \x13–\x1c — цифры 0–9,
# \x1d — двоеточие, \x87 — тире-пункт.
_CTRL_MAP = {
    0x03: " ",
    0x05: '"',
    0x08: "%",
    0x0F: ",",
    0x11: ".",
    0x12: "№",
    0x1D: ":",
    0x1E: ",",
    0x87: "-",
}
for _i in range(10):
    _CTRL_MAP[0x13 + _i] = str(_i)


def looks_like_mojibake(text: str) -> bool:
    """Доля символов U+0230–U+02AF выше порога при почти нулевой кириллице."""
    sample = text[:20000]
    if not sample:
        return False
    mojibake = len(_MOJIBAKE_RE.findall(sample))
    if mojibake < 20:
        return False
    cyrillic = len(_CYRILLIC_RE.findall(sample))
    return mojibake > max(20, cyrillic * 4)


def demap(text: str) -> str:
    """Возвращает текст с восстановленной кириллицей; неизвестное — как есть.

    U+028B (ʋ) и литеральная U+0461 (ѡ) кодируют две буквы: внутри слова
    это «у» («дрʋгих» → «других»), отдельно стоящая — знак номера.
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0x230 <= code <= 0x2AF:
            out.append(chr(code + 0x1D6))
        elif ch == "©":
            out.append("«")
        elif ch == "ª":
            out.append("»")
        elif ch == "ѡ":
            out.append("ѡ")
        elif ch == "±":
            out.append("-")
        elif code in _CTRL_MAP:
            out.append(_CTRL_MAP[code])
        else:
            out.append(ch)
    demapped = "".join(out)
    demapped = re.sub(r"(?<=[А-Яа-яЁё])ѡ(?=[А-Яа-яЁё])", "у", demapped)
    return demapped.replace("ѡ", "№")


def ensure_readable(text: str) -> str:
    """Демаппит текст, если он похож на сбитую кодировку."""
    return demap(text) if looks_like_mojibake(text) else text
