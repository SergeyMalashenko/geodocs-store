"""Адаптер РГИС МО: документные файлы информационных карточек.

Discovery на слоях РГИС — ответственность pyrgis-mcp: он регистрирует
ref'ы с source=rgis и source_object_id=<id карточки>. Этот адаптер добывает
файлы уже известной карточки: geoportal/card/files → документные файлы
(без `.sig`, графической части и файлов больше лимита размера). Поиска по
реквизитам у портала нет: search → []. Логика перенесена из
pyrgis-agents (RgisCardFilesProvider) на чистый httpx.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from .base import BasePortalAdapter, Candidate, DocQuery, PortalError

_BASE_URL = "https://rgis.mosreg.ru/v3/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://rgis.mosreg.ru/v3/",
}
_DEFAULT_MAX_FILE_BYTES = 30 * 1024 * 1024
_CARD_ID_RE = re.compile(r"(\d{5,})")
_SAFE_NAME_RE = re.compile(r"[^\w.-]+")

_SKIPPED_SECTION_MARKERS = (
    "пзз графическая часть",
    "дополнительная информация",
    "аго",
)
_SKIPPED_EXTENSIONS = {"sig"}
_SIZE_PATTERN = re.compile(r"[-+]?\d[\d\s\u00a0]*(?:[.,]\d+)?")
_SIZE_MULTIPLIERS = {
    "б": 1,
    "кб": 1024,
    "мб": 1024**2,
    "гб": 1024**3,
}


class CardFile(BaseModel):
    """Файл информационной карточки объекта (geoportal/card/files)."""

    title: str
    ext: str | None = None
    mime: str | None = None
    size: str | None = None
    uri: str


class CardFileSection(BaseModel):
    """Раздел дерева файлов информационной карточки объекта."""

    title: str | None = None
    children: list[CardFileSection | CardFile] = []


@dataclass(frozen=True)
class CardFileDownload:
    """Скачанный документный файл карточки."""

    title: str
    section: str | None
    uri: str
    content: bytes


def _parse_size_bytes(value: str | None) -> int | None:
    """«8702 Кб» / «31 Кб» / «5 Мб» / «123 Б» → байты; непарсибельное → None."""
    if not value:
        return None
    normalized = " ".join(value.replace("\u00a0", " ").split()).casefold()
    match = _SIZE_PATTERN.search(normalized)
    if match is None:
        return None
    number = match.group(0).replace(" ", "").replace(",", ".")
    try:
        amount = float(number)
    except ValueError:
        return None
    rest = normalized[match.end() :].strip()
    unit = rest.split(" ", 1)[0] if rest else "б"
    multiplier = _SIZE_MULTIPLIERS.get(unit)
    if multiplier is None:
        return None
    return int(amount * multiplier)


def select_files(
    sections: list[CardFileSection], *, max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
) -> list[tuple[CardFile, str | None]]:
    """Файлы дерева карточки, подлежащие скачиванию, с названиями разделов."""
    selected: list[tuple[CardFile, str | None]] = []

    def walk(nodes: list[Any], section_title: str | None) -> None:
        for node in nodes:
            if isinstance(node, CardFileSection):
                title = node.title or ""
                folded = title.casefold()
                if any(marker in folded for marker in _SKIPPED_SECTION_MARKERS):
                    continue
                walk(node.children, title)
            elif isinstance(node, CardFile):
                ext = (node.ext or "").casefold().lstrip(".")
                if ext in _SKIPPED_EXTENSIONS or node.title.casefold().endswith(".sig"):
                    continue
                size_bytes = _parse_size_bytes(node.size)
                if size_bytes is not None and size_bytes > max_file_bytes:
                    continue
                selected.append((node, section_title))

    walk(sections, None)
    return selected


def _card_id(candidate: Candidate) -> int:
    """Id карточки РГИС из meta кандидата (object_id/card_id) или его URL."""
    for key in ("object_id", "card_id"):
        value = candidate.meta.get(key)
        if value is not None and str(value).isdigit():
            return int(str(value))
    match = _CARD_ID_RE.search(candidate.url)
    if match is None:
        raise PortalError(f"rgis: не могу вытащить id карточки из {candidate.url}")
    return int(match.group(1))


class RgisAdapter(BasePortalAdapter):
    """Файлы карточек РГИС МО (geoportal/card/files); поиска нет."""

    name = "rgis"

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        super().__init__(client)
        if client is None:
            self._client = httpx.AsyncClient(
                base_url=_BASE_URL,
                headers=_HEADERS,
                timeout=60.0,
                follow_redirects=True,
            )
            self._owns_client = True
        self.max_file_bytes = max_file_bytes
        self._authorized = False

    async def search(self, query: DocQuery) -> list[Candidate]:
        """Поиска по реквизитам у РГИС нет: discovery делает pyrgis-mcp."""
        return []

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        """Документные файлы карточки в dest_dir; возвращает первый из них."""
        downloads = await self.fetch_card_documents(_card_id(candidate))
        if not downloads:
            raise PortalError(f"{self.name}: на карточке нет документных файлов")
        dest_dir.mkdir(parents=True, exist_ok=True)
        first: Path | None = None
        for item in downloads:
            path = dest_dir / _SAFE_NAME_RE.sub("_", item.title)
            path.write_bytes(item.content)
            if first is None:
                first = path
        assert first is not None
        return first

    async def card_files(self, object_id: int) -> list[CardFileSection]:
        """Дерево файлов карточки; пустой список = у карточки нет файлов."""
        data = await self._get_json(
            "swagger/geoportal/card/files", params={"id": object_id}
        )
        if data is None:
            return []
        return [CardFileSection.model_validate(item) for item in data]

    async def download_file(self, uri: str) -> bytes:
        """Скачивает файл карточки по относительному uri из card_files."""
        await self._ensure_session()
        response = await self._client.get(uri.removeprefix("./"))
        if response.status_code == 401:
            self._authorized = False
            await self._ensure_session()
            response = await self._client.get(uri.removeprefix("./"))
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PortalError(f"{self.name}: не удалось скачать {uri}: {exc}") from exc
        return response.content

    async def fetch_card_documents(self, object_id: int) -> list[CardFileDownload]:
        """Документные файлы карточки: отбор select_files + скачивание.

        Ошибка отдельного файла не фатальна: он пропускается.
        """
        sections = await self.card_files(object_id)
        downloads: list[CardFileDownload] = []
        for card_file, section in select_files(
            sections, max_file_bytes=self.max_file_bytes
        ):
            try:
                content = await self.download_file(card_file.uri)
            except PortalError:
                continue
            downloads.append(
                CardFileDownload(
                    title=card_file.title,
                    section=section,
                    uri=card_file.uri,
                    content=content,
                )
            )
        return downloads

    async def _ensure_session(self) -> None:
        """POST peekaboo — выпуск сессионной cookie геопортала."""
        if self._authorized:
            return
        try:
            response = await self._client.post("peekaboo")
        except httpx.HTTPError as exc:
            raise PortalError(
                f"{self.name}: сессия геопортала не установлена: {exc}"
            ) from exc
        if response.status_code != 200:
            raise PortalError(f"{self.name}: peekaboo вернул HTTP {response.status_code}")
        self._authorized = True

    async def _get_json(self, path: str, params: dict[str, Any]) -> Any:
        """GET JSON с сессией; 401 → перевыпуск сессии и один повтор."""
        await self._ensure_session()
        response = await self._client.get(path, params=params)
        if response.status_code == 401:
            self._authorized = False
            await self._ensure_session()
            response = await self._client.get(path, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PortalError(f"{self.name}: не удалось получить {path}: {exc}") from exc
        data = response.json()
        if (
            isinstance(data, dict)
            and isinstance(data.get("status"), int)
            and data["status"] >= 400
        ):
            raise PortalError(f"{self.name}: API вернул статус {data['status']}: {path}")
        return data


__all__ = [
    "CardFile",
    "CardFileDownload",
    "CardFileSection",
    "RgisAdapter",
    "select_files",
]
