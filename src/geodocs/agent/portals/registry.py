"""Реестр адаптеров порталов: register_portal / get_portal / list_portals.

Встроенные адаптеры регистрируются в portals/__init__.py. Новый портал
добавляется кодом без правок ядра: адаптер по протоколу PortalAdapter →
register_portal("имя", фабрика) → доступен в find_document / import_document.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import PortalAdapter

_PORTALS: dict[str, Callable[[], PortalAdapter]] = {}


def register_portal(name: str, factory: Callable[[], PortalAdapter]) -> None:
    """Регистрирует адаптер портала; перезапись зарегистрированного имени — ValueError."""
    if name in _PORTALS:
        raise ValueError(f"портал {name!r} уже зарегистрирован")
    _PORTALS[name] = factory


def get_portal(name: str) -> PortalAdapter:
    """Адаптер по имени; неизвестное имя — KeyError со списком известных."""
    factory = _PORTALS.get(name)
    if factory is None:
        known = ", ".join(sorted(_PORTALS)) or "нет зарегистрированных"
        raise KeyError(f"неизвестный портал {name!r} (зарегистрированные: {known})")
    return factory()


def list_portals() -> list[str]:
    """Имена всех зарегистрированных порталов."""
    return sorted(_PORTALS)
