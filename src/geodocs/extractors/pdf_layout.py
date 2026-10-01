"""Извлечение таблицы ВРИ из текстового PDF через ``pdftotext -layout``.

Потоковый текст PDF раскладывается колонками; наименования ВРИ часто
перенесены на соседние строки относительно строки-ядра (номер + код +
числовые параметры). Парсер: локация секции зоны → скан строк-ядер по
коду вида → сборка наименований из соседних строк (окно ±2, стоп по
ключам шапки) → разбор параметров по колонкам.
"""

from __future__ import annotations

import re
import shutil
import statistics
import subprocess
from pathlib import Path

from ._rows import HEADER_KEYWORDS, assemble_item, is_code, split_row_tokens
from .models import ExtractionOutcome, VriItem, VriTable
from .mojibake import ensure_readable

EXTRACTOR_NAME = "vri-pdf-layout@1"

_PDFTOTEXT = shutil.which("pdftotext")
_SCAN_CYRILLIC_THRESHOLD = 200
_ZONE_ANY_RE = re.compile(
    r"^\s*[А-ЯA-ZЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-яA-Za-z]?\s*[-–—]?\s+\S*\s*ЗОНА\b"
)
_CHAPTER_RE = re.compile(r"^\s*(Глава|Статья)\s+\d", re.IGNORECASE)
_CORE_RE = re.compile(
    r"^\s*(?:(?P<row>\d{1,2})[.)]?\s+)?"
    r"(?P<mid>.*?)"
    r"(?P<code>(?<![\d.])(?:\d{1,2}\.){1,3}\d\*{0,4}(?![\d.*]))"
    r"(?P<tail>.*)$"
)
_PAGE_NUMBER_RE = re.compile(r"^\s*\d{1,4}\s*$")
_NAME_WINDOW = 2

# Токоны чисто колоночной шапки: строка целиком из них — не кандидат в имена.
# Словарь покрывает усечённые переносы заголовков колонок («Макси», «застро»).
_COLUMN_TOKENS = {
    "min",
    "max",
    "п/п",
    "№",
    "№п/п",
    "код",
    "обозначение",
    "числовое",
    "ври",
    "наименование",
    "квм",
    "м",
    "предельные",
    "размеры",
    "минимальные",
    "отступы",
    "процент",
    "процента",
    "застройки",
    "максимальный",
    "требования",
    "архитектурно",
    "архитектурному",
    "градострои",
    "градостроительному",
    "регулированию",
    "облику",
    "облика",
    "этажности",
    "этажей",
    "высоты",
    "участка",
    "горизонтали",
    "класса",
    "энергетической",
    "эффективности",
    "макси",
    "застро",
    "зависим",
    "над",
    "количества",
    "надземных",
    "подземных",
    "земельного",
    "тельному",
    "настоящих",
}
_TOKEN_STRIP_RE = re.compile(r"[^0-9a-zа-яё№/\-]+")


def _norm_column_token(token: str) -> str:
    """Токон колонки в сравнимый вид: без пунктуации и звёздочек яркости."""
    return _TOKEN_STRIP_RE.sub("", token.casefold())


def _all_column_tokens(tokens: list[str]) -> bool:
    """Все токены строки — из колоночной шапки (после нормализации)."""
    return bool(tokens) and all(
        _norm_column_token(token) in _COLUMN_TOKENS for token in tokens
    )

_SECTION_STOP_KEYWORDS = (
    "основные виды разрешенного использования",
    "вспомогательные виды",
    "условно разрешенные виды",
)

# Ключевые слова шапки как гибкие по пробелам регэкспы (позиция совпадения
# на сырой строке нужна для обрезки переносов вида «Имя … Не подлежат»).
_HEADER_KEYWORD_RES = tuple(
    re.compile(r"\s+".join(re.escape(word) for word in keyword.split()), re.IGNORECASE)
    for keyword in HEADER_KEYWORDS
)


def _section_start_re(zone_code: str) -> re.Pattern[str]:
    """Заголовок секции запрошенной зоны: «Ж-2 - ЗОНА…»/«СХ-2 ЗОНА…».

    После кода запрещены буквы/цифры/точина — «Ж-2Б» и «Ж-2.1» не совпадут.
    """
    return re.compile(
        rf"^\s*{re.escape(zone_code)}(?![\w.])\s*(?:[-–—]\s*)?ЗОНА\b",
        re.IGNORECASE,
    )


def run_pdftotext(file_path: Path) -> str:
    """Весь текст PDF через внешний poppler-бинарник."""
    result = subprocess.run(
        [_PDFTOTEXT, "-layout", str(file_path), "-"],
        capture_output=True,
        timeout=180,
        check=False,
    )
    return result.stdout.decode("utf-8", errors="replace")


def _count_cyrillic(text: str) -> int:
    return sum(1 for ch in text if "А" <= ch <= "я" or ch in "Ёё")


def locate_section(text: str, zone_code: str) -> tuple[int, int] | None:
    """Границы секции зоны: (start, end) или None.

    Конец — следующий заголовок зоны/главы или EOF.
    """
    lines = text.split("\n")
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    start_re = _section_start_re(zone_code)
    start_line = None
    for index, line in enumerate(lines):
        if start_re.match(line):
            start_line = index
            break
    if start_line is None:
        return None
    end_line = len(lines)
    for index in range(start_line + 1, len(lines)):
        line = lines[index]
        if _CHAPTER_RE.match(line) or _ZONE_ANY_RE.match(line):
            end_line = index
            break
    return offsets[start_line], offsets[end_line - 1] + len(lines[end_line - 1])


def _cut_at_keyword(line: str) -> str | None:
    """Отрезает хвост строки от первого ключевого слова шапки («Не подлежат…»).

    Переносы вида «Для индивидуального        Не подлежат» смешивают имя и
    маркер колонки отступов: имя — левая часть до ключевого слова.
    """
    cut: int | None = None
    for pattern in _HEADER_KEYWORD_RES:
        match = pattern.search(line)
        if match is not None and (cut is None or match.start() < cut):
            cut = match.start()
    if cut is None:
        return " ".join(line.split())
    left = " ".join(line[:cut].split())
    return left or None


def _name_fragment(line: str) -> str | None:
    """Текст фрагмента-переноса наименования ВРИ или None для прочих строк."""
    stripped = line.strip()
    if not stripped:
        return None
    if _PAGE_NUMBER_RE.match(stripped):
        return None
    tokens = stripped.casefold().split()
    if _all_column_tokens(tokens):
        return None
    if any(is_code(token) for token in stripped.split()):
        return None
    folded = " ".join(stripped.split()).casefold()
    if any(keyword in folded for keyword in _SECTION_STOP_KEYWORDS):
        return None
    text = _cut_at_keyword(line)
    if text is None:
        return None
    leftover = text.casefold().split()
    if _all_column_tokens(leftover):
        return None
    return text


def _zone_description(section_lines: list[str], zone_code: str) -> str | None:
    expected = zone_code.casefold()
    parts: list[str] = []
    for line in section_lines:
        folded = " ".join(line.split()).casefold()
        if folded.startswith(expected) and not parts:
            continue
        if any(keyword in folded for keyword in _SECTION_STOP_KEYWORDS):
            break
        if _ZONE_ANY_RE.match(line) or _CHAPTER_RE.match(line):
            break
        if line.strip():
            parts.append(" ".join(line.split()))
        if len(" ".join(parts)) > 2000:
            break
    if not parts:
        return None
    return " ".join(parts)[:2000]


def _starts_upper(text: str) -> bool:
    """Первая буква фрагмента заглавная (начало нового наименования)."""
    for ch in text:
        if ch.isalpha():
            return ch.isupper()
    return False


def parse_section(section: str, zone_code: str, source_file: str) -> VriTable:
    """Парсит строки таблицы ВРИ внутри секции зоны."""
    lines = section.split("\n")
    cores: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = _CORE_RE.match(line)
        if match is None:
            continue
        if not is_code(match.group("code")):
            continue
        cores.append((index, match))

    core_positions = [index for index, _ in cores]
    # Колонка кода таблицы: переносы наименований всегда левее неё,
    # шапки параметров («min max», «надземных этажей участка (м)») — правее.
    # Медиана, т.к. вспомогательные списки («Связь - 6.8») дают ранние выбросы.
    code_columns = [match.start("code") for _, match in cores]
    code_col = int(statistics.median(code_columns)) if code_columns else 10**6
    # Маркеры подразделов таблицы; фрагмент по другую сторону маркера от ядра
    # — это текст описания зоны, а не перенос наименования.
    marker_positions = [
        index
        for index, line in enumerate(lines)
        if any(
            keyword in " ".join(line.split()).casefold()
            for keyword in _SECTION_STOP_KEYWORDS
        )
    ]
    # Переносы-имена распределяются по ближайшему ядру; при равенстве
    # дистанций фрагмент отдаётся ядру ниже (переносы висят над своей строкой).
    assigned: dict[int, list[tuple[int, str]]] = {index: [] for index in core_positions}
    for position, line in enumerate(lines):
        if position in assigned or _CORE_RE.match(line) is not None:
            continue
        text = _name_fragment(line)
        if text is None:
            continue
        indent = len(line) - len(line.lstrip())
        if indent >= code_col - 2:
            continue
        # Направление: перенос с заглавной — начало нового имени, вешается
        # на ядро ниже; со строчной — продолжение, цепляется к ядру выше.
        # При пустом направленном пуле (имя целиком над/под одним ядром) —
        # откат к ближайшему ядру без учёта регистра.
        upper = _starts_upper(text)
        candidates = [
            c
            for c in core_positions
            if abs(c - position) <= _NAME_WINDOW
            and not any(
                min(position, c) < mp < max(position, c)
                for mp in marker_positions
            )
        ]
        if not candidates:
            continue
        directed = [c for c in candidates if (c > position) == upper]
        pool = directed or candidates
        nearest = min(
            pool,
            key=lambda c: (abs(c - position), 0 if upper == (c > position) else 1),
        )
        assigned[nearest].append((position, text))

    items: list[VriItem] = []
    for index, match in cores:
        inline = match.group("mid").strip()
        wrapped = sorted(assigned[index])
        above = [text for pos, text in wrapped if pos < index]
        below = [text for pos, text in wrapped if pos > index]
        # читающийся порядок: переносы сверху, инлайн-имя из ядра, переносы снизу
        name_parts = above + ([" ".join(inline.split())] if inline else []) + below
        tokens = split_row_tokens(match.group("tail"))
        item = assemble_item(
            row=match.group("row") or "",
            code=match.group("code"),
            name=" ".join(name_parts) if name_parts else None,
            tokens=tokens,
            raw="\n".join(
                [lines[i] for i in range(max(0, index - 2), min(len(lines), index + 3))]
            ).strip(),
        )
        items.append(item)

    counts = {"rows_total": len(items), "rows_parsed": len(items)}
    confidence = 0.8 if items else 0.5
    header_line = lines[0].strip()
    name_match = re.match(
        r"^\s*[А-ЯA-ZЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-яA-Za-z]?\s*[-–—]?\s*(?P<name>ЗОНА\b.*)$",
        header_line,
        re.IGNORECASE,
    )
    description = _zone_description(lines, zone_code)
    return VriTable(
        zone_code=zone_code,
        zone_name=name_match.group("name").strip() if name_match else None,
        zone_description=description,
        items=items,
        counts=counts,
        source_file=source_file,
        confidence=confidence,
    )


def extract_pdf(file_path: Path, zone_code: str) -> ExtractionOutcome:
    """Полный пайплайн: pdftotext → демаппинг → секция → строки таблицы."""
    if _PDFTOTEXT is None:
        return ExtractionOutcome(
            status="no_pdftotext",
            detail="pdftotext не найден в PATH",
            extractor=EXTRACTOR_NAME,
        )
    try:
        text = run_pdftotext(file_path)
    except (OSError, subprocess.SubprocessError) as exc:
        return ExtractionOutcome(
            status="error", detail=f"pdftotext не выполнен: {exc}", extractor=EXTRACTOR_NAME
        )
    if _count_cyrillic(text) < _SCAN_CYRILLIC_THRESHOLD:
        return ExtractionOutcome(
            status="scan_pdf",
            detail="текстовый слой отсутствует (скан)",
            extractor=EXTRACTOR_NAME,
        )
    text = ensure_readable(text)
    span = locate_section(text, zone_code)
    if span is None:
        return ExtractionOutcome(
            status="no_section",
            detail=f"секция зоны {zone_code} не найдена",
            extractor=EXTRACTOR_NAME,
        )
    section = text[span[0] : span[1]]
    table = parse_section(section, zone_code, str(file_path))
    return ExtractionOutcome(status="extracted", table=table, extractor=EXTRACTOR_NAME)
