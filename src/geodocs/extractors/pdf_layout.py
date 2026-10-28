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
from typing import Any

from ._rows import HEADER_KEYWORDS, assemble_item, is_code, split_row_tokens
from .models import ExtractionOutcome, VriItem, VriTable, parse_number
from .mojibake import ensure_readable

EXTRACTOR_NAME = "vri-pdf-layout@1"

_PDFTOTEXT = shutil.which("pdftotext")
_SCAN_CYRILLIC_THRESHOLD = 200
_ZONE_ANY_RE = re.compile(
    r"^\s*[А-ЯA-ZЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-яA-Za-z]?(?:-[А-ЯA-ZЁ]{1,2})?"
    r"\s*[-–—]?\s+\S*\s*ЗОНА\b"
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
# Голый код зоны на отдельной строке — заголовок секции КРТ-подобного layout
# (без слова «ЗОНА»): «КРТ-3-63» над таблицей параметров и списком ВРИ.
_ZONE_BARE_RE = re.compile(
    r"^\s*[А-ЯA-ZЁ]{1,4}(?:-\d+(?:\.\d+)?)+(?:-[А-ЯA-ZЁ]{1,2})?\s*$"
)

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

    После кода запрещены буквы/цифры/точина и дефисный суффикс — «Ж-2Б»,
    «Ж-2.1» и «СХ-2-Р» для «СХ-2» не совпадут. Между кодом и словом ЗОНА
    допускаются уточняющие слова («СПЕЦИАЛИЗИРОВАННАЯ ЗОНА …»).
    """
    return re.compile(
        rf"^\s*{re.escape(zone_code)}(?![\w.]|-[А-ЯA-ZЁA-Za-zа-яё])"
        rf"\s*(?:[-–—]\s*)?(?:\S+\s+)*?ЗОНА\b",
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

    Конец — следующий заголовок зоны/главы или EOF. Если заголовка
    «КОД — ЗОНА …» нет, ищется голый код на отдельной строке (секции
    КРТ-подобного layout); тогда концом считается и следующий голый код.
    """
    lines = text.split("\n")
    offsets: list[int] = []
    position = 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    start_re = _section_start_re(zone_code)
    start_line = None
    bare = False
    for index, line in enumerate(lines):
        if start_re.match(line):
            start_line = index
            break
    if start_line is None:
        target = zone_code.strip().casefold()
        for index, line in enumerate(lines):
            if _ZONE_BARE_RE.match(line) and line.strip().casefold() == target:
                start_line = index
                bare = True
                break
    if start_line is None:
        return None
    end_line = len(lines)
    for index in range(start_line + 1, len(lines)):
        line = lines[index]
        if _CHAPTER_RE.match(line) or _ZONE_ANY_RE.match(line):
            end_line = index
            break
        if bare and _ZONE_BARE_RE.match(line):
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
    # Строки до первого маркера подраздела — заголовок и описание зоны;
    # коды вида «СХ-2.1-Р» в них не табличные, в ядра не берём.
    first_marker = next(
        (
            index
            for index, line in enumerate(lines)
            if any(
                keyword in " ".join(line.split()).casefold()
                for keyword in _SECTION_STOP_KEYWORDS
            )
        ),
        None,
    )
    cores: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        if first_marker is not None and index < first_marker:
            continue
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
        r"^\s*[А-ЯA-ZЁ]{1,4}-\d+(?:\.\d+)*[А-Яа-яA-Za-z]?(?:-[А-ЯA-ZЁ]{1,2})?"
        r"\s*[-–—]?\s*(?P<name>(?:\S+\s+)*ЗОНА\b.*)$",
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


# ---------------------------------------------------------------------------
# КРТ-подобный layout: голый код зоны → описание → таблица параметров
# («Наименование параметра — Значение») → список ВРИ «код — наименование»
# без числовых колонок (ПЗЗ Рузского муниципального округа, стр. 316+).
# ---------------------------------------------------------------------------

KRT_EXTRACTOR_NAME = "vri-pdf-layout-krt@1"

_VRI_LIST_MARKER_RE = re.compile(r"виды\s+разрешенного\s+использования", re.IGNORECASE)
_KRT_LIST_LINE_RE = re.compile(
    r"^(?P<code>(?:\d{1,2}\.)+\d{1,2}\*{0,4})\s+(?P<name>\S.*)$"
)
_KRT_TYPE_PREFIX_RE = re.compile(
    r"^\s*(?:основные|условно-?|разрешенные|вспомогательные)\s*:?\s+", re.IGNORECASE
)
_KRT_HEADER_TOKENS = {"тип", "ври"}


def _krt_items(list_lines: list[str]) -> list[VriItem]:
    """Список ВРИ «код — наименование»; переносы цепляются к предыдущему."""
    items: list[VriItem] = []
    for line in list_lines:
        stripped = line.strip()
        if not stripped or _PAGE_NUMBER_RE.match(stripped):
            continue
        if _VRI_LIST_MARKER_RE.search(stripped):
            continue
        content = " ".join(_KRT_TYPE_PREFIX_RE.sub("", line).split())
        tokens = content.casefold().split()
        if not content or all(token in _KRT_HEADER_TOKENS for token in tokens):
            continue
        match = _KRT_LIST_LINE_RE.match(content)
        if match is not None:
            items.append(
                VriItem(
                    row="",
                    code=match.group("code"),
                    name=match.group("name"),
                    raw=content,
                )
            )
        elif items:
            previous = items[-1]
            name = f"{previous.name} {content}" if previous.name else content
            items[-1] = previous.model_copy(
                update={"name": name, "raw": f"{previous.raw}\n{content}"}
            )
    return items


def _krt_zone_params(region: str) -> dict[str, Any]:
    """Зональные параметры из таблицы «параметр — значение» (текст в плоскую)."""
    flat = " ".join(region.split())
    params: dict[str, Any] = {}
    pct = re.search(r"не более\s+(\d+(?:[.,]\d+)?)\s*%", flat, re.IGNORECASE)
    floors = re.search(r"не более\s+(\d+)\s*этаж", flat, re.IGNORECASE)
    if pct is not None:
        params["building_percentage"] = (
            f"{floors.group(1)} эт. – {pct.group(1)}%"
            if floors is not None
            else f"{pct.group(1)}%"
        )
    margin = re.search(
        r"отступ\w*\s+от\s+границ\W{0,60}?(\d+(?:[.,]\d+)?)\s*метра",
        flat,
        re.IGNORECASE,
    )
    if margin is not None:
        params["margin"] = parse_number(margin.group(1))
    area = re.search(
        r"минимальн\w*\s+размер\w*\s+земельного\s+участка\s+(\d+(?:[.,]\d+)?)\s*га",
        flat,
        re.IGNORECASE,
    )
    if area is not None:
        params["area_min"] = int(float(area.group(1).replace(",", ".")) * 10_000)
    qualifier = re.search(r"«([^»]+)»", flat)
    if qualifier is not None:
        params["qualifier"] = qualifier.group(1)
    return params


def _attach_zone_params(items: list[VriItem], params: dict[str, Any]) -> None:
    """Зональные параметры — на ВРИ, названный в квалификаторе «…».

    Квалификатор в тексте склеен из двух колонок («для индивидуального
    жилищного участка строительства»), поэтому совпадение — по перекрытию
    словаря: ≥ 80% слов наименования ВРИ есть среди слов квалификатора.
    """
    qualifier = params.get("qualifier")
    if not isinstance(qualifier, str):
        return
    qwords = set(qualifier.casefold().split())
    if not qwords:
        return
    best: tuple[float, int] | None = None
    for index, item in enumerate(items):
        name_words = set((item.name or "").casefold().split())
        if not name_words:
            continue
        overlap = len(qwords & name_words) / len(name_words)
        if overlap >= 0.8 and (best is None or overlap > best[0]):
            best = (overlap, index)
    if best is None:
        return
    updates = {key: value for key, value in params.items() if key != "qualifier"}
    items[best[1]] = items[best[1]].model_copy(update=updates)


def _krt_description(lines: list[str]) -> str | None:
    """Описание зоны: текст между заголовком и таблицей параметров."""
    parts: list[str] = []
    for line in lines:
        folded = " ".join(line.split())
        if not folded:
            continue
        lowered = folded.casefold()
        if "наименование параметра" in lowered or lowered.startswith("№"):
            break
        parts.append(folded)
    if not parts:
        return None
    return " ".join(parts)[:2000]


def parse_krt_section(section: str, zone_code: str, source_file: str) -> VriTable:
    """Секция КРТ-layout: описание, зональные параметры и список ВРИ."""
    lines = section.split("\n")
    marker_index = next(
        (i for i, line in enumerate(lines) if _VRI_LIST_MARKER_RE.search(line)),
        len(lines),
    )
    description = _krt_description(lines[1:marker_index])
    params = _krt_zone_params("\n".join(lines[1:marker_index]))
    items = _krt_items(lines[marker_index:])
    if params and items:
        _attach_zone_params(items, params)
    return VriTable(
        zone_code=zone_code,
        zone_description=description,
        items=items,
        counts={"rows_total": len(items), "rows_parsed": len(items)},
        source_file=source_file,
        confidence=0.6,
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
    if _ZONE_BARE_RE.match(section.split("\n", 1)[0]):
        krt_table = parse_krt_section(section, zone_code, str(file_path))
        if krt_table.items:
            return ExtractionOutcome(
                status="extracted", table=krt_table, extractor=KRT_EXTRACTOR_NAME
            )
    table = parse_section(section, zone_code, str(file_path))
    return ExtractionOutcome(status="extracted", table=table, extractor=EXTRACTOR_NAME)
