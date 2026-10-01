"""Модели хранилища градостроительных документов."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


class DocType(str, Enum):
    """Тип градостроительного документа."""

    PZZ = "pzz"
    GENERAL_PLAN = "general_plan"
    GPZU = "gpzu"
    PLANNING_PROJECT = "planning_project"
    SURVEYING_PROJECT = "surveying_project"
    ZOUIT_REGIME = "zouit_regime"
    UNKNOWN = "unknown"


class DocRole(str, Enum):
    """Роль версии: базовый документ, изменение или автономный документ."""

    BASE = "base"
    AMENDMENT = "amendment"
    SINGLE = "single"
    UNKNOWN = "unknown"


class SourceName(str, Enum):
    """Источник, обнаруживший документ."""

    RGIS = "rgis"
    NSPD = "nspd"
    CNTD = "cntd"
    MUNICIPAL = "municipal"


class FetchStatus(str, Enum):
    """Состояние загрузки файла версии."""

    PENDING = "pending"
    DOWNLOADED = "downloaded"
    SEARCH_FAILED = "search_failed"
    DOWNLOAD_FAILED = "download_failed"
    NOT_FOUND = "not_found"


class ExtractionKind(str, Enum):
    """Вид извлечённого из документа фрагмента."""

    VRI_TABLE = "vri_table"
    ZONE_DESCRIPTION = "zone_description"
    HEIGHT_LIMITS = "height_limits"
    ZOUIT_REGIME_TEXT = "zouit_regime_text"


class ExtractionOrigin(str, Enum):
    """Откуда взят текст: из скачанного файла или из атрибутов провайдера."""

    FETCHED_FILE = "fetched_file"
    PROVIDER_ATTRIBUTES = "provider_attributes"


class DocumentRef(BaseModel):
    """Ссылка на документ, пришедшая из discovery-провайдера."""

    municipality: str
    doc_type: DocType
    number: str
    version_date: str | None = None
    role: DocRole = DocRole.UNKNOWN
    title: str | None = None
    issuer: str | None = None
    region_code: str | None = None
    source: SourceName
    source_object_id: str | None = None
    amendment_number: str | None = None


class DocumentRecord(BaseModel):
    """Документ муниципалитета без привязки к редакции."""

    id: int
    municipality: str
    doc_type: DocType
    number: str
    title: str | None = None
    issuer: str | None = None
    region_code: str | None = None
    created_at: datetime


class DocumentVersionRecord(BaseModel):
    """Редакция документа и состояние её файла."""

    id: int
    document_id: int
    version_date: str
    role: DocRole = DocRole.UNKNOWN
    amendment_number: str | None = None
    title: str | None = None
    issuer: str | None = None
    file_path: str | None = None
    sha256: str | None = None
    source_url: str | None = None
    source_provider: SourceName | None = None
    fetch_status: FetchStatus = FetchStatus.PENDING
    fetched_at: datetime | None = None
    created_at: datetime


class ExtractionRecord(BaseModel):
    """Извлечённый из версии фрагмент (таблица ВРИ, описание зоны и т.п.)."""

    id: int
    version_id: int
    zone_code: str
    kind: ExtractionKind
    origin: ExtractionOrigin
    payload: dict[str, Any]
    extractor: str
    confidence: float | None = None
    created_at: datetime


class VersionFileRecord(BaseModel):
    """Файл версии документа (текстовая часть, постановление, регламент)."""

    id: int
    version_id: int
    title: str | None = None
    section: str | None = None
    file_path: str
    sha256: str | None = None
    size_bytes: int | None = None
    created_at: datetime


class ParcelDocumentLink(BaseModel):
    """Связь кадастрового номера с версией документа."""

    cadastral_number: str
    version_id: int
    first_seen_at: datetime
    last_seen_at: datetime
