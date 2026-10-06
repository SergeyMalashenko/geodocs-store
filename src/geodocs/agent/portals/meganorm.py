"""Адаптер meganorm.ru — нормативная база СНиП/ГОСТ/СП.

ВАЖНО (трап прошлых прогонов): карточки meganorm отдают HTML полного текста
(`/Data2/<...>.htm`), а не файлы. Для муниципальных ПЗЗ/генпланов это почти
всегда ложное срабатывание: meganorm — строительные нормы, а не муниципальные
ПЗЗ. fetch отказывается сохранять HTML и поднимает PortalError; файл
добывается только если карточка содержит прямую ссылку на pdf/doc/zip.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._html import file_links, filename_from_response, html_to_text, snippet_around
from .base import BasePortalAdapter, Candidate, DocQuery, PortalError

_BASE = "https://meganorm.ru"
_SEARCH_URL = _BASE + "/Search2/search"  # GET ?q=<текст>
# Карточки и тексты: /Data2/<раздел>/<id>/<docid>.htm
_CARD_RE = re.compile(r"/Data2/\d+/\d+/\d+\.htm", re.IGNORECASE)


class MeganormAdapter(BasePortalAdapter):
    """Поиск по нормативной базе meganorm; fetch — только реальных файлов."""

    name = "meganorm"

    async def search(self, query: DocQuery) -> list[Candidate]:
        text = f"{query.number} {query.title or ''}".strip()
        response = await self._get(_SEARCH_URL, params={"q": text})
        return self._parse_results(response.text, query)

    def _parse_results(self, html: str, query: DocQuery) -> list[Candidate]:
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for match in _CARD_RE.finditer(html):
            path = match.group(0)
            if path in seen:
                continue
            seen.add(path)
            page_text = html_to_text(
                html[max(0, match.start() - 400) : match.end() + 400]
            )
            snippet = snippet_around(page_text, query.number) or page_text[:200]
            candidates.append(
                Candidate(
                    url=_BASE + path,
                    title=page_text[:120],
                    portal=self.name,
                    meta={"snippet": snippet, "html_fulltext": True},
                )
            )
            if len(candidates) >= 10:
                break
        return candidates

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        """HTML-карточку в файл не превращаем: либо файл по ссылке, либо отказ."""
        response = await self._get(candidate.url)
        page = response.text
        files = file_links(page, candidate.url)
        if not files:
            raise PortalError(
                f"{self.name}: карточка {candidate.url} — HTML полного текста,"
                " файла документа на ней нет; для ПЗЗ/генпланов meganorm —"
                " ложное срабатывание, пропусти этот результат"
            )
        _label, url = files[0]
        return await self._download(url, dest_dir)

    async def _download(self, url: str, dest_dir: Path) -> Path:
        response = await self._get(url)
        content_type = response.headers.get("content-type", "")
        if "html" in content_type.lower():
            raise PortalError(f"{self.name}: {url} отдал HTML, а не файл")
        name = filename_from_response(url, dict(response.headers))
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / name
        target.write_bytes(response.content)
        return target


__all__ = ["MeganormAdapter"]
