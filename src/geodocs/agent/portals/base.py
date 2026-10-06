"""Порталы-доноры документов: общие модели и протокол адаптера.

Адаптер портала умеет две вещи: найти кандидатов по реквизитам документа
(search) и добыть файл кандидата в каталог (fetch). HTTP — через
httpx.AsyncClient, который адаптер получает извне: в тестах клиент
подменяется respx, сети в юнит-тестах нет.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx


class PortalError(RuntimeError):
    """Портал недоступен, изменил вёрстку или не отдал файл."""


@dataclass(frozen=True)
class DocQuery:
    """Реквизиты искомого документа."""

    municipality: str
    doc_type: str  # pzz | general_plan | gpzu | zouit_regime | ...
    number: str
    version_date: str
    title: str | None = None


@dataclass(frozen=True)
class Candidate:
    """Найденный документ: прямая ссылка на карточку/файл + метаданные."""

    url: str
    title: str
    portal: str
    meta: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PortalAdapter(Protocol):
    """Сменный адаптер портала-донора нормативных документов."""

    name: str

    async def search(self, query: DocQuery) -> list[Candidate]:
        """Кандидаты по реквизитам; пустой список = не найдено."""
        ...

    async def fetch(self, candidate: Candidate, dest_dir: Path) -> Path:
        """Скачивает файл кандидата в dest_dir; только PDF/DOCX/сканы, не HTML."""
        ...


class BasePortalAdapter:
    """Базовый адаптер: держит httpx-клиент, закрывает его, если сам создал."""

    name: str = "base"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "geodocs-agent/0.2 (+municipal-docs-search)"},
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.get(url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise PortalError(f"{self.name}: не удалось получить {url}: {exc}") from exc
        return response
