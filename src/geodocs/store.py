"""SQLite-хранилище градостроительных документов с upsert-семантикой."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    DocRole,
    DocType,
    DocumentRecord,
    DocumentRef,
    DocumentVersionRecord,
    ExtractionKind,
    ExtractionOrigin,
    ExtractionRecord,
    FetchStatus,
    FileRecord,
    ParcelDocumentLink,
    SourceName,
    VersionFileRecord,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    municipality TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    number TEXT NOT NULL,
    title TEXT,
    issuer TEXT,
    region_code TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (municipality, doc_type, number)
);
CREATE TABLE IF NOT EXISTS document_versions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    version_date TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'unknown',
    amendment_number TEXT,
    title TEXT,
    issuer TEXT,
    file_path TEXT,
    sha256 TEXT,
    source_url TEXT,
    source_provider TEXT,
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    fetched_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, version_date)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_sha256
    ON document_versions (sha256) WHERE sha256 IS NOT NULL;
CREATE TABLE IF NOT EXISTS document_sources (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES document_versions (id),
    source TEXT NOT NULL,
    source_object_id TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    UNIQUE (version_id, source, source_object_id)
);
CREATE TABLE IF NOT EXISTS version_files (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES document_versions (id),
    title TEXT,
    section TEXT,
    file_path TEXT NOT NULL,
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (version_id, sha256)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_version_files_sha256
    ON version_files (sha256) WHERE sha256 IS NOT NULL;
CREATE TABLE IF NOT EXISTS parcel_documents (
    cadastral_number TEXT NOT NULL,
    version_id INTEGER NOT NULL REFERENCES document_versions (id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (cadastral_number, version_id)
);
CREATE TABLE IF NOT EXISTS extractions (
    id INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES document_versions (id),
    zone_code TEXT NOT NULL,
    kind TEXT NOT NULL,
    origin TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    extractor TEXT NOT NULL,
    confidence REAL,
    created_at TEXT NOT NULL,
    UNIQUE (version_id, zone_code, kind)
);
"""

_TABLES = (
    "documents",
    "document_versions",
    "document_sources",
    "version_files",
    "parcel_documents",
    "extractions",
)

_CHUNK_SIZE = 1024 * 1024
_UNKNOWN_VERSION_DATE = "unknown"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: str) -> str:
    """Схлопывает пробелы: часть нормализованной идентичности документа."""
    return " ".join(value.split())


def _safe_component(value: str) -> str:
    """Оставляет в компоненте пути только буквы, цифры и `._-`."""
    return re.sub(r"[^\w.-]+", "_", value) or "_"


def _agent_file_section(name: str) -> str:
    """Раздел version_files по имени файла: карта/регламент/текст/файл."""
    folded = name.casefold()
    if "карт" in folded:
        return "карта"
    if "регламент" in folded:
        return "регламент"
    if any(marker in folded for marker in ("изменен", "решение", "постановлен", "генплан")):
        return "текст"
    return "файл"


def _primary_agent_file(files: list[FileRecord]) -> FileRecord:
    """Первичный файл редакции: PDF с «изменен»/«решение» в имени, иначе первый PDF, иначе первый файл."""
    def is_pdf(record: FileRecord) -> bool:
        return record.path.casefold().endswith(".pdf")

    def named(record: FileRecord) -> bool:
        name = (record.title or Path(record.path).name).casefold()
        return "изменен" in name or "решение" in name

    for record in files:
        if is_pdf(record) and named(record):
            return record
    for record in files:
        if is_pdf(record):
            return record
    return files[0]


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), row, strict=True))


def _to_extraction(row: sqlite3.Row) -> ExtractionRecord:
    data = _row_dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    return ExtractionRecord(**data)


class DocumentStore:
    """Общее хранилище документов муниципалитетов для обоих MCP-сервисов."""

    def __init__(self, db_path: str | Path, files_dir: str | Path | None = None) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._files_dir = (
            Path(files_dir) if files_dir is not None else self._db_path.parent / "files"
        )
        self._files_dir.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self) -> None:
        """(Пере)открывает соединение; безопасно после close()."""
        self._conn = sqlite3.connect(str(self._db_path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._conn:
            self._conn.executescript(_SCHEMA)

    @classmethod
    def from_env(cls, env_var: str = "GEODOCS_HOME") -> DocumentStore:
        """Создаёт хранилище в каталоге из env (по умолчанию `~/.geodocs`)."""
        home = Path(os.environ.get(env_var, str(Path.home() / ".geodocs"))).expanduser()
        return cls(home / "geodocs.sqlite3", files_dir=home / "files")

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def files_dir(self) -> Path:
        return self._files_dir

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._connect()
        assert self._conn is not None
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> DocumentStore:  # noqa: PYI034  # нет typing.Self на py3.10
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def upsert_document(
        self,
        *,
        municipality: str,
        doc_type: DocType,
        number: str,
        title: str | None = None,
        issuer: str | None = None,
        region_code: str | None = None,
    ) -> int:
        """Идемпотентно создаёт документ и возвращает его id."""
        municipality = _normalize(municipality)
        number = _normalize(number)
        doc_type = DocType(doc_type)
        with self.connection:
            self.connection.execute(
                "INSERT INTO documents"
                " (municipality, doc_type, number, title, issuer, region_code, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    municipality,
                    doc_type.value,
                    number,
                    title,
                    issuer,
                    region_code,
                    _utcnow(),
                ),
            )
            row = self.connection.execute(
                "SELECT id FROM documents"
                " WHERE municipality = ? AND doc_type = ? AND number = ?",
                (municipality, doc_type.value, number),
            ).fetchone()
        return int(row["id"])

    def upsert_version(
        self,
        document_id: int,
        *,
        version_date: str,
        role: DocRole = DocRole.UNKNOWN,
        amendment_number: str | None = None,
        title: str | None = None,
        issuer: str | None = None,
    ) -> int:
        """Идемпотентно создаёт редакцию документа и возвращает её id."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO document_versions"
                " (document_id, version_date, role, amendment_number, title, issuer,"
                "  created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    document_id,
                    version_date,
                    DocRole(role).value,
                    amendment_number,
                    title,
                    issuer,
                    _utcnow(),
                ),
            )
            row = self.connection.execute(
                "SELECT id FROM document_versions"
                " WHERE document_id = ? AND version_date = ?",
                (document_id, version_date),
            ).fetchone()
        return int(row["id"])

    def register_ref(self, ref: DocumentRef) -> int:
        """Регистрирует ref из discovery и возвращает id версии."""
        document_id = self.upsert_document(
            municipality=ref.municipality,
            doc_type=ref.doc_type,
            number=ref.number,
            title=ref.title,
            issuer=ref.issuer,
            region_code=ref.region_code,
        )
        version_id = self.upsert_version(
            document_id,
            version_date=ref.version_date or _UNKNOWN_VERSION_DATE,
            role=ref.role,
            amendment_number=ref.amendment_number,
            title=ref.title,
            issuer=ref.issuer,
        )
        if ref.source_object_id:
            self.add_source(version_id, ref.source, ref.source_object_id)
        return version_id

    def add_source(
        self, version_id: int, source: SourceName, source_object_id: str
    ) -> None:
        """Фиксирует, что версию обнаружил данный источник."""
        with self.connection:
            self.connection.execute(
                "INSERT INTO document_sources"
                " (version_id, source, source_object_id, discovered_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (version_id, SourceName(source).value, source_object_id, _utcnow()),
            )

    def link_parcel(self, cadastral_number: str, version_id: int) -> None:
        """Привязывает кадастровый номер к версии, обновляя last_seen_at."""
        now = _utcnow()
        with self.connection:
            self.connection.execute(
                "INSERT INTO parcel_documents"
                " (cadastral_number, version_id, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT (cadastral_number, version_id)"
                " DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (cadastral_number, version_id, now, now),
            )

    def find_version(
        self,
        *,
        municipality: str,
        doc_type: DocType,
        number: str,
        version_date: str,
    ) -> DocumentVersionRecord | None:
        """Ищет редакцию по идентичности документа (проверка «уже в базе»)."""
        row = self.connection.execute(
            "SELECT v.* FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id"
            " WHERE d.municipality = ? AND d.doc_type = ? AND d.number = ?"
            " AND v.version_date = ?",
            (
                _normalize(municipality),
                DocType(doc_type).value,
                _normalize(number),
                version_date,
            ),
        ).fetchone()
        return DocumentVersionRecord(**_row_dict(row)) if row is not None else None

    def save_file(
        self,
        version_id: int,
        data: bytes,
        *,
        filename: str,
        source_url: str | None = None,
        source_provider: SourceName,
        title: str | None = None,
        section: str | None = None,
    ) -> Path:
        """Сохраняет файл версии с дедупликацией по содержимому.

        Каждый вызов добавляет запись в `version_files` (набор файлов версии)
        и обновляет первичный файл редакции в `document_versions`. Если другая
        версия уже хранит файл с тем же sha256, новый файл не создаётся:
        версия привязывается к существующему пути, а её sha256 остаётся NULL —
        владельцем хэша (и уникального индекса) остаётся версия, сохранившая
        файл первой.
        """
        row = self.connection.execute(
            "SELECT d.municipality, d.doc_type, d.number, v.version_date"
            " FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id"
            " WHERE v.id = ?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"версия {version_id} не найдена")
        target_dir = (
            self._files_dir
            / _safe_component(row["municipality"])
            / _safe_component(f"{row['doc_type']}_{row['number']}")
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / _safe_component(f"{row['version_date']}_{filename}")

        digest = hashlib.sha256()
        with path.open("wb") as fh:
            for offset in range(0, len(data), _CHUNK_SIZE):
                chunk = data[offset : offset + _CHUNK_SIZE]
                fh.write(chunk)
                digest.update(chunk)
        sha256 = digest.hexdigest()

        update_sql = (
            "UPDATE document_versions SET file_path = ?, sha256 = ?, source_url = ?,"
            " source_provider = ?, fetch_status = ?, fetched_at = ? WHERE id = ?"
        )

        def update(file_path: Path, stored_sha256: str | None) -> None:
            self.connection.execute(
                update_sql,
                (
                    str(file_path),
                    stored_sha256,
                    source_url,
                    SourceName(source_provider).value,
                    FetchStatus.DOWNLOADED.value,
                    _utcnow(),
                    version_id,
                ),
            )

        def add_version_file(file_path: Path, stored_sha256: str | None) -> None:
            if stored_sha256 is not None:
                self.connection.execute(
                    "INSERT INTO version_files"
                    " (version_id, title, section, file_path, sha256, size_bytes,"
                    "  created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (version_id, sha256) DO NOTHING",
                    (
                        version_id,
                        title,
                        section,
                        str(file_path),
                        stored_sha256,
                        len(data),
                        _utcnow(),
                    ),
                )
            else:
                self.connection.execute(
                    "INSERT INTO version_files"
                    " (version_id, title, section, file_path, sha256, size_bytes,"
                    "  created_at)"
                    " SELECT ?, ?, ?, ?, NULL, ?, ?"
                    " WHERE NOT EXISTS ("
                    "  SELECT 1 FROM version_files"
                    "  WHERE version_id = ? AND file_path = ?)",
                    (
                        version_id,
                        title,
                        section,
                        str(file_path),
                        len(data),
                        _utcnow(),
                        version_id,
                        str(file_path),
                    ),
                )

        with self.connection:
            existing = self.connection.execute(
                "SELECT id, file_path FROM version_files"
                " WHERE sha256 = ? AND version_id != ?",
                (sha256, version_id),
            ).fetchone()
            if existing is not None and existing["file_path"]:
                path.unlink(missing_ok=True)
                final_path = Path(existing["file_path"])
                update(final_path, None)
                add_version_file(final_path, None)
            else:
                final_path = path
                try:
                    update(final_path, sha256)
                    add_version_file(final_path, sha256)
                except sqlite3.IntegrityError:
                    # Гонка: другой писатель успел сохранить тот же контент.
                    existing = self.connection.execute(
                        "SELECT id, file_path FROM version_files"
                        " WHERE sha256 = ? AND version_id != ?",
                        (sha256, version_id),
                    ).fetchone()
                    if existing is None or not existing["file_path"]:
                        raise
                    path.unlink(missing_ok=True)
                    final_path = Path(existing["file_path"])
                    update(final_path, None)
                    add_version_file(final_path, None)
        return final_path

    def set_fetch_status(
        self,
        version_id: int,
        status: FetchStatus,
        source_url: str | None = None,
    ) -> None:
        """Обновляет состояние загрузки версии."""
        with self.connection:
            self.connection.execute(
                "UPDATE document_versions SET fetch_status = ?,"
                " source_url = COALESCE(?, source_url) WHERE id = ?",
                (FetchStatus(status).value, source_url, version_id),
            )

    def record_agent_fetch(
        self,
        version_id: int,
        *,
        files: list[FileRecord],
        source_url: str | None,
        fetched_at: str,
        source_provider: SourceName = SourceName.KIMI_AGENT,
    ) -> None:
        """Фиксирует результат агентного яруса одной транзакцией.

        Первичным файлом редакции становится первый PDF с «изменен»/«решение»
        в имени, иначе первый PDF, иначе первый файл. Набор `version_files`
        версии заменяется целиком, provenance дополняется источником
        `source_provider` (kimi-agent/hermes-agent — добыт исполнителем,
        manual — принят из inbox вручную через recover). `source_url=None`
        означает, что URL неизвестен: прежний source_url версии сохраняется,
        запись provenance не добавляется (source_object_id обязателен в схеме).
        Повторный вызов с теми же файлами идемпотентен.
        """
        if not files:
            raise ValueError("files не должен быть пустым")
        provider = SourceName(source_provider)
        primary = _primary_agent_file(files)
        with self.connection:
            self.connection.execute(
                "UPDATE document_versions SET file_path = ?, sha256 = ?,"
                " source_url = COALESCE(?, source_url),"
                " source_provider = ?, fetch_status = ?,"
                " fetched_at = ? WHERE id = ?",
                (
                    primary.path,
                    primary.sha256,
                    source_url,
                    provider.value,
                    FetchStatus.DOWNLOADED.value,
                    fetched_at,
                    version_id,
                ),
            )
            self.connection.execute(
                "DELETE FROM version_files WHERE version_id = ?", (version_id,)
            )
            for record in files:
                name = record.title or Path(record.path).name
                self.connection.execute(
                    "INSERT INTO version_files"
                    " (version_id, title, section, file_path, sha256, size_bytes,"
                    "  created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        version_id,
                        record.title,
                        record.section or _agent_file_section(name),
                        record.path,
                        record.sha256,
                        record.size,
                        _utcnow(),
                    ),
                )
            if source_url is not None:
                self.connection.execute(
                    "INSERT INTO document_sources"
                    " (version_id, source, source_object_id, discovered_at)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (version_id, provider.value, source_url, fetched_at),
                )

    def upsert_extraction(
        self,
        version_id: int,
        *,
        zone_code: str,
        kind: ExtractionKind,
        origin: ExtractionOrigin,
        payload: dict[str, Any],
        extractor: str,
        confidence: float | None = None,
    ) -> int:
        """Идемпотентно сохраняет извлечённый фрагмент, заменяя payload."""
        payload_json = json.dumps(payload, ensure_ascii=False)
        kind = ExtractionKind(kind)
        with self.connection:
            self.connection.execute(
                "INSERT INTO extractions"
                " (version_id, zone_code, kind, origin, payload_json, extractor,"
                "  confidence, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (version_id, zone_code, kind) DO UPDATE SET"
                " payload_json = excluded.payload_json,"
                " extractor = excluded.extractor,"
                " confidence = excluded.confidence,"
                " created_at = excluded.created_at",
                (
                    version_id,
                    zone_code,
                    kind.value,
                    ExtractionOrigin(origin).value,
                    payload_json,
                    extractor,
                    confidence,
                    _utcnow(),
                ),
            )
            row = self.connection.execute(
                "SELECT id FROM extractions"
                " WHERE version_id = ? AND zone_code = ? AND kind = ?",
                (version_id, zone_code, kind.value),
            ).fetchone()
        return int(row["id"])

    def get_extraction(
        self, version_id: int, *, zone_code: str, kind: ExtractionKind
    ) -> ExtractionRecord | None:
        """Возвращает извлечённый фрагмент версии, если он есть."""
        row = self.connection.execute(
            "SELECT * FROM extractions"
            " WHERE version_id = ? AND zone_code = ? AND kind = ?",
            (version_id, zone_code, ExtractionKind(kind).value),
        ).fetchone()
        return _to_extraction(row) if row is not None else None

    def extractions_for_version(self, version_id: int) -> list[ExtractionRecord]:
        """Все извлечённые фрагменты версии."""
        rows = self.connection.execute(
            "SELECT * FROM extractions WHERE version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()
        return [_to_extraction(row) for row in rows]

    def get_document(self, document_id: int) -> DocumentRecord | None:
        """Документ по id."""
        row = self.connection.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        return DocumentRecord(**_row_dict(row)) if row is not None else None

    def sources_for_version(self, version_id: int) -> list[str]:
        """Источники, обнаружившие версию (provenance)."""
        rows = self.connection.execute(
            "SELECT source FROM document_sources WHERE version_id = ?"
            " ORDER BY source",
            (version_id,),
        ).fetchall()
        return [str(row["source"]) for row in rows]

    def source_refs_for_version(self, version_id: int) -> list[tuple[str, str]]:
        """Пары (source, source_object_id), обнаружившие версию (provenance)."""
        rows = self.connection.execute(
            "SELECT source, source_object_id FROM document_sources"
            " WHERE version_id = ? ORDER BY source, source_object_id",
            (version_id,),
        ).fetchall()
        return [(str(row["source"]), str(row["source_object_id"])) for row in rows]

    def files_for_version(self, version_id: int) -> list[VersionFileRecord]:
        """Все файлы версии (текстовая часть, НПА, зональные регламенты)."""
        rows = self.connection.execute(
            "SELECT * FROM version_files WHERE version_id = ? ORDER BY id",
            (version_id,),
        ).fetchall()
        return [VersionFileRecord(**_row_dict(row)) for row in rows]

    def documents_for_parcel(self, cadastral_number: str) -> list[DocumentVersionRecord]:
        """Все версии документов, привязанные к кадастровому номеру."""
        rows = self.connection.execute(
            "SELECT v.* FROM document_versions v"
            " JOIN parcel_documents p ON p.version_id = v.id"
            " WHERE p.cadastral_number = ? ORDER BY v.id",
            (cadastral_number,),
        ).fetchall()
        return [DocumentVersionRecord(**_row_dict(row)) for row in rows]

    def unlinked_versions_for_municipality(
        self, municipality: str, cadastral_number: str
    ) -> list[DocumentVersionRecord]:
        """Версии документов муниципалитета, ещё не привязанные к кадастровому номеру."""
        rows = self.connection.execute(
            "SELECT v.* FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id"
            " WHERE d.municipality = ?"
            " AND NOT EXISTS (SELECT 1 FROM parcel_documents p"
            " WHERE p.version_id = v.id AND p.cadastral_number = ?)"
            " ORDER BY v.id",
            (municipality, cadastral_number),
        ).fetchall()
        return [DocumentVersionRecord(**_row_dict(row)) for row in rows]

    def links_for_version(self, version_id: int) -> list[ParcelDocumentLink]:
        """Все кадастровые номера, привязанные к версии."""
        rows = self.connection.execute(
            "SELECT * FROM parcel_documents WHERE version_id = ?"
            " ORDER BY cadastral_number",
            (version_id,),
        ).fetchall()
        return [ParcelDocumentLink(**_row_dict(row)) for row in rows]

    def versions_for_document(self, document_id: int) -> list[DocumentVersionRecord]:
        """Все редакции документа."""
        rows = self.connection.execute(
            "SELECT * FROM document_versions WHERE document_id = ? ORDER BY id",
            (document_id,),
        ).fetchall()
        return [DocumentVersionRecord(**_row_dict(row)) for row in rows]

    def stats(self) -> dict[str, int]:
        """Счётчики записей по всем таблицам."""
        return {
            table: int(
                self.connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            )
            for table in _TABLES
        }
