"""Адаптер ФГИС ТП (fgistp.economy.gov.ru) — документы территориального планирования.

ЭВРИСТИКА, ТРЕБУЕТ ПРОВЕРКИ (см. README). Публичного поискового API нет; с
сентября 2026 доступ к материалам закрыт без привязки к органам власти
(geo-risk.ru/blog/fgis-tp-zakryli-dostup-chto-delat). Схема: агент находит
карточку документа через веб-поиск (`/ais/of1?id=<HEX>` или
`/lk/#/document-show/<id>`), адаптер парсит карточку и вытаскивает ссылки
на файлы (pdf/zip). Если доступ закрыт — честный PortalError.
"""

from __future__ import annotations

import re
from pathlib import Path

from ._html import file_links, filename_from_response
from .base import BasePortalAdapter, Candidate, DocQuery, PortalError

_BASE = "https://fgistp.economy.gov.ru"
_CARD_URL = _BASE + "/ais/of1"
_ID_RE = re.compile(r"[?&#]id=([0-9A-Fa-f]{16,})")


class FgistpAdapter(BasePortalAdapter):
    """Карточки ФГИС ТП: парсинг ссылок на файлы с размещённых документов."""

    name = "fgistp"

    async def search(self, query: DocQuery) -> list[Candidate]:
        """Поиска по сайту нет: агент приносит URL карточки из веб-поиска."""
        return []

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        document_id = self._document_id(candidate.url)
        if document_id is None:
            raise PortalError(
                f"{self.name}: ожидаю карточку вида {_CARD_URL}?id=<HEX>,"
                f" получил {candidate.url}"
            )
        response = await self._get(_CARD_URL, params={"id": document_id})
        page = response.text
        if "доступ" in page.lower() and "ограничен" in page.lower():
            raise PortalError(
                f"{self.name}: доступ к материалам ФГИС ТП ограничен"
                " (нужна учётная запись органа власти)"
            )
        files = file_links(page, _CARD_URL)
        if not files:
            raise PortalError(
                f"{self.name}: на карточке id={document_id} нет ссылок на файлы;"
                " документ может быть доступен только после входа"
            )
        _label, url = files[0]
        return await self._download(url, dest_dir, document_id)

    def _document_id(self, url: str) -> str | None:
        match = _ID_RE.search(url)
        return match.group(1) if match else None

    async def _download(self, url: str, dest_dir: Path, document_id: str) -> Path:
        response = await self._get(url)
        content_type = response.headers.get("content-type", "")
        if "html" in content_type.lower():
            raise PortalError(f"{self.name}: {url} отдал HTML, а не файл")
        name = filename_from_response(url, dict(response.headers))
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / name
        target.write_bytes(response.content)
        return target


__all__ = ["FgistpAdapter"]
