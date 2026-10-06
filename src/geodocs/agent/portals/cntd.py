"""Адаптер docs.cntd.ru — полные тексты нормативных документов.

Поиск — по поисковой выдаче сайта (`/search?q=`), парсинг карточек
`/document/<id>`. Полный текст — публичным блок-API: документ отдаётся
HTML-фрагментами `content/text/block/{n}`, склеиваем в один файл .html
(экстракторы geodocs умеют читать HTML-таблицы ВРИ). Бесплатная часть —
публичный текст; платные приложения/сканы не добываем.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._html import html_to_text, snippet_around
from .base import BasePortalAdapter, Candidate, DocQuery, PortalError

_BASE = "https://docs.cntd.ru"
_SEARCH_URL = _BASE + "/search"
_META_URL = "https://api.docs.cntd.ru/document/{}"
_SIZE_URL = _BASE + "/api/document/{}/content/text/blocks/size"
_BLOCK_URL = _BASE + "/api/document/{}/content/text/block/{}?query=&strict=strict"
_DOCUMENT_RE = re.compile(r"/document/([A-Za-z0-9_-]+)")
_MAX_BLOCKS = 500


class CntdAdapter(BasePortalAdapter):
    """Поиск и полный текст docs.cntd.ru."""

    name = "cntd"

    async def search(self, query: DocQuery) -> list[Candidate]:
        text = " ".join(part for part in (query.number, query.title) if part)
        response = await self._get(_SEARCH_URL, params={"q": text})
        return self._parse_results(response.text, query)

    def _parse_results(self, html: str, query: DocQuery) -> list[Candidate]:
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for document_id in _DOCUMENT_RE.findall(html):
            if document_id in seen:
                continue
            seen.add(document_id)
            page_text = html_to_text(html)
            snippet = snippet_around(page_text, query.number) or ""
            candidates.append(
                Candidate(
                    url=f"{_BASE}/document/{document_id}",
                    title=f"CNTD {document_id}",
                    portal=self.name,
                    meta={"document_id": document_id, "snippet": snippet},
                )
            )
            if len(candidates) >= 10:
                break
        return candidates

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        """Полный текст документа: блоки склеиваются в один HTML-файл."""
        document_id = str(candidate.meta.get("document_id") or "")
        if not document_id:
            match = _DOCUMENT_RE.search(candidate.url)
            if not match:
                raise PortalError(
                    f"{self.name}: не могу вытащить id из {candidate.url}"
                )
            document_id = match.group(1)
        html = await self._full_text_html(document_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"cntd_{document_id}.html"
        target.write_text(html, encoding="utf-8")
        return target

    async def _full_text_html(self, document_id: str) -> str:
        indexes = await self._block_indexes(document_id)
        parts: list[str] = []
        for index in indexes[:_MAX_BLOCKS]:
            try:
                response = await self._get(_BLOCK_URL.format(document_id, index))
            except PortalError:
                continue  # битый блок не валит документ
            parts.append(response.text)
        if not parts:
            raise PortalError(
                f"{self.name}: ни один текстовый блок не скачан для {document_id}"
            )
        header = f"<!-- источник: {_BASE}/document/{document_id} -->\n"
        return header + "\n".join(parts)

    async def _block_indexes(self, document_id: str) -> list[int]:
        response = await self._get(_SIZE_URL.format(document_id))
        try:
            payload = response.json()
        except ValueError as exc:
            raise PortalError(
                f"{self.name}: размер документа {document_id} не JSON"
            ) from exc
        data = payload.get("data") or {}
        content = data.get("content") or []
        return [
            block["block_index"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("block_index"), int)
        ]


__all__ = ["CntdAdapter"]
