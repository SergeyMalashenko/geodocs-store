"""Универсальный муниципальный адаптер: fetch страницы без поиска.

search всегда пуст: URL страницы агент приносит сам из веб-поиска или
навигации по сайту администрации. fetch отдаёт (путь к текстовой выжимке,
кандидата дополненного ссылками на файлы): текст страницы — в .txt рядом,
файлы pdf/docx — в dest_dir.
"""

from __future__ import annotations

from pathlib import Path

from ._html import file_links, html_to_text
from .base import BasePortalAdapter, Candidate, DocQuery, PortalError

_FILE_KINDS = (".pdf", ".doc", ".docx", ".zip", ".rar")


class MunicipalAdapter(BasePortalAdapter):
    """Читатель муниципальных страниц: текст + ссылки на файлы документов."""

    name = "municipal"

    async def search(self, query: DocQuery) -> list[Candidate]:
        return []

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        """Скачивает страницу: файлы — в dest_dir, текст — в <name>.txt."""
        response = await self._get(candidate.url)
        content_type = response.headers.get("content-type", "").lower()
        dest_dir.mkdir(parents=True, exist_ok=True)

        if any(
            candidate.url.lower().split("?")[0].endswith(ext) for ext in _FILE_KINDS
        ):
            # Кандидат и есть файл.
            name = candidate.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            target = dest_dir / name
            target.write_bytes(response.content)
            return target

        if "html" not in content_type and "text" not in content_type:
            name = (
                candidate.url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "page.bin"
            )
            target = dest_dir / name
            target.write_bytes(response.content)
            return target

        page = response.text
        for _label, url in file_links(page, candidate.url):
            try:
                file_response = await self._get(url)
            except PortalError:
                continue
            file_type = file_response.headers.get("content-type", "").lower()
            if "html" in file_type:
                continue  # псевдо-ссылка на файл, ведущая на HTML
            name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0] or "document.bin"
            (dest_dir / name).write_bytes(file_response.content)

        text_path = dest_dir / "page_text.txt"
        text_path.write_text(
            f"источник: {candidate.url}\n\n{html_to_text(page)}", encoding="utf-8"
        )
        return text_path


__all__ = ["MunicipalAdapter"]
