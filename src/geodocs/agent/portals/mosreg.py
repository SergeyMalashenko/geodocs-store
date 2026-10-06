"""Адаптер портала Московской области (data.mosreg.ru / mosreg.ru).

Поиск ПЗЗ/генпланов округов области через открытый портал данных; карточки
парсятся на ссылки «скачать» (pdf/zip). Вёрстка периодически меняется —
парсер защищён отсутствием кандидатов, а не падением.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._html import file_links, filename_from_response, html_to_text, snippet_around
from .base import BasePortalAdapter, Candidate, DocQuery, PortalError

_BASE = "https://data.mosreg.ru"
_SEARCH_URL = _BASE + "/search"
_CARD_RE = re.compile(r"/(?:dataset|document|view)/[A-Za-z0-9_-]+", re.IGNORECASE)


class MosregAdapter(BasePortalAdapter):
    """Поиск и скачивание документов с порталов Московской области."""

    name = "mosreg"

    async def search(self, query: DocQuery) -> list[Candidate]:
        text = " ".join(
            part for part in (query.municipality, query.number, query.title) if part
        )
        response = await self._get(_SEARCH_URL, params={"q": text})
        return self._parse_results(response.text, query)

    def _parse_results(self, html: str, query: DocQuery) -> list[Candidate]:
        candidates: list[Candidate] = []
        seen: set[str] = set()
        page_text = html_to_text(html)
        for match in _CARD_RE.finditer(html):
            path = match.group(0)
            if path in seen:
                continue
            seen.add(path)
            candidates.append(
                Candidate(
                    url=_BASE + path,
                    title=snippet_around(page_text, query.number, 160) or path,
                    portal=self.name,
                    meta={"snippet": snippet_around(page_text, query.number)},
                )
            )
            if len(candidates) >= 10:
                break
        return candidates

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        response = await self._get(candidate.url)
        page = response.text
        files = file_links(page, candidate.url)
        if not files:
            raise PortalError(
                f"{self.name}: на карточке {candidate.url} нет ссылок на файлы"
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


__all__ = ["MosregAdapter"]
