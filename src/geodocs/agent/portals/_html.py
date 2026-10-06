"""HTML-утилиты порталов без сторонних зависимостей: текст и ссылки."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

_LINK_RE = re.compile(r"(?:href|src)=[\"']([^\"']+)[\"']", re.IGNORECASE)
_FILE_EXT_RE = re.compile(
    r"\.(pdf|docx?|zip|rar|png|jpe?g|tif?f)(\?|#|$)", re.IGNORECASE
)
_DISPOSITION_RE = re.compile(r'filename\*?=["\']?([^"\';]+)', re.IGNORECASE)


def filename_from_response(url: str, headers: dict[str, str] | None = None) -> str:
    """Имя файла: Content-Disposition → ?name= → последний сегмент пути."""
    headers = headers or {}
    disposition = headers.get("content-disposition", "")
    match = _DISPOSITION_RE.search(disposition)
    if match:
        return match.group(1).strip()
    query_name = parse_qs(urlparse(url).query).get("name")
    if query_name and query_name[0]:
        return query_name[0]
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return name or "document.bin"


def extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Все (href, абсолютный url) со страницы, относительные — к base_url."""
    return [(href, urljoin(base_url, href)) for href in _LINK_RE.findall(html)]


def file_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Ссылки на файлы документов (pdf/doc/zip/rar): (подпись-рядом, url).

    Подпись — текст ближайшего атрибута title/содержимого у ссылки, что есть.
    """
    found: list[tuple[str, str]] = []
    for match in re.finditer(
        r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        href, label_html = match.group(1), match.group(2)
        if not _FILE_EXT_RE.search(href):
            continue
        label = re.sub(r"<[^>]+>", " ", label_html)
        label = " ".join(label.split())[:200]
        found.append((label, urljoin(base_url, href)))
    return found


def html_to_text(html: str) -> str:
    """Чистый текст страницы: скрипты/стили вырезаны, теги — в пробелы."""
    stripped = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    stripped = re.sub(r"(?s)<!--.*?-->", " ", stripped)
    text = re.sub(r"<[^>]+>", " ", stripped)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def snippet_around(text: str, needle: str, width: int = 120) -> str | None:
    """Фрагмент текста вокруг первого вхождения needle (для meta кандидатов)."""
    index = text.casefold().find(needle.casefold())
    if index < 0:
        return None
    start = max(0, index - width // 2)
    return text[start : start + width]
