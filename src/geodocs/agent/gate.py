"""Детерминированная верификация файлов, заявленных агентом (только std-lib)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from pydantic import BaseModel

# Минимальные пороги размера: меньше — почти наверняка ошибка/заглушка.
_SIZE_THRESHOLDS = {
    "pdf": 30_000,
    "jpeg": 30_000,
    "png": 30_000,
    "docx": 10_000,
    "doc": 10_000,
    "rar": 1_000,
    "zip": 1_000,
}

_UNKNOWN = "unknown"
_MISSING = "missing"
_READ_HEAD = 8

# Расширения файлов, которые гейт способен принять (соответствуют kind'ам
# _SIZE_THRESHOLDS). Всё прочее (page_text.txt, HTML-оглавления) — не документ.
DOCUMENT_SUFFIXES = frozenset(
    {".pdf", ".jpeg", ".jpg", ".png", ".docx", ".doc", ".rar", ".zip"}
)


class FileVerdict(BaseModel):
    """Вердикт по одному файлу: определённый тип, размер и итоговая валидность."""

    path: str
    size: int
    kind: str
    ok: bool
    reason: str


def _detect_kind(path: Path, head: bytes) -> str:
    """Определяет тип файла по сигнатуре (без python-magic)."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"Rar!"):
        return "rar"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "doc"  # legacy OLE2 (Word 97-2003), порталы МО до сих пор публикуют
    if head.startswith(b"PK"):
        if zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    if "word/document.xml" in archive.namelist():
                        return "docx"
            except zipfile.BadZipFile:
                pass
        return "zip"
    return _UNKNOWN


def _verify_one(path: Path) -> FileVerdict:
    if not path.is_file():
        return FileVerdict(
            path=str(path), size=0, kind=_MISSING, ok=False,
            reason="файл отсутствует",
        )
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(_READ_HEAD)
    kind = _detect_kind(path, head)
    if kind in (_UNKNOWN, _MISSING):
        return FileVerdict(
            path=str(path), size=size, kind=kind, ok=False,
            reason="неизвестный тип файла",
        )
    threshold = _SIZE_THRESHOLDS[kind]
    if size < threshold:
        return FileVerdict(
            path=str(path), size=size, kind=kind, ok=False,
            reason=f"размер {size} меньше порога {threshold}",
        )
    return FileVerdict(path=str(path), size=size, kind=kind, ok=True, reason="ok")


def verify_files(paths: list[Path]) -> list[FileVerdict]:
    """Проверяет каждый файл: сигнатура типа и порог размера для этого типа."""
    return [_verify_one(Path(path)) for path in paths]


def gate_pass(verdicts: list[FileVerdict]) -> bool:
    """Гейт приёма результата агента.

    Правило: вердиктов хотя бы один и все вердикты `ok` — то есть среди
    заявленных агентом файлов есть ≥1 валидный и ни один не провалил проверку
    (отсутствующие и битые/подозрительно маленькие файлы гейт не пропускают).
    """
    return bool(verdicts) and all(verdict.ok for verdict in verdicts)
