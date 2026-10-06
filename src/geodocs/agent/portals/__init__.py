"""Порталы-доноры документов: базовые модели, реестр и встроенные адаптеры."""

from .base import Candidate, DocQuery, PortalAdapter, PortalError
from .registry import get_portal, list_portals, register_portal

__all__ = [
    "Candidate",
    "DocQuery",
    "PortalAdapter",
    "PortalError",
    "get_portal",
    "list_portals",
    "register_portal",
]


def _register_builtin_portals() -> None:
    from .cntd import CntdAdapter
    from .fgistp import FgistpAdapter
    from .meganorm import MeganormAdapter
    from .mosreg import MosregAdapter
    from .municipal import MunicipalAdapter
    from .pravo import PravoAdapter

    register_portal("cntd", CntdAdapter)
    register_portal("fgistp", FgistpAdapter)
    register_portal("meganorm", MeganormAdapter)
    register_portal("mosreg", MosregAdapter)
    register_portal("municipal", MunicipalAdapter)
    register_portal("pravo", PravoAdapter)


_register_builtin_portals()
