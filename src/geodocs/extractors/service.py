"""Диспетчер извлечения таблиц ВРИ: файлы версии → обработчик → geodocs."""

from __future__ import annotations

from pathlib import Path

from ..models import (
    ExtractionKind,
    ExtractionOrigin,
    ExtractionRecord,
    VersionFileRecord,
)
from ..store import DocumentStore
from .docx_tables import extract_docx
from .html_tables import extract_html
from .models import ExtractionOutcome, VriTable
from .pdf_layout import extract_pdf


def extract_vri_from_file(
    file_path: str | Path, *, filename: str | None = None, zone_code: str
) -> ExtractionOutcome:
    """Диспетчер по расширению файла: docx → pdf → html → error."""
    path = Path(file_path)
    suffix = (path.suffix or Path(filename or "").suffix).casefold()
    if suffix == ".docx":
        return extract_docx(path, zone_code)
    if suffix == ".pdf":
        return extract_pdf(path, zone_code)
    if suffix in {".html", ".htm"}:
        return extract_html(path, zone_code)
    return ExtractionOutcome(
        status="error",
        detail=f"неподдерживаемый тип файла: {suffix or '(нет расширения)'}",
        extractor="vri-dispatcher@1",
    )


class VriExtractor:
    """Извлекает таблицы ВРИ из файлов версии и сохраняет их в geodocs."""

    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def _candidates(self, files: list[VersionFileRecord], zone: str) -> list[VersionFileRecord]:
        """Приоритет источников: регламент зоны по title → общий регламент → docx → html → pdf."""
        ranked: list[tuple[int, VersionFileRecord]] = []
        zone_folded = zone.casefold()
        for position, file in enumerate(files):
            title = (file.title or Path(file.file_path).name).casefold()
            suffix = Path(file.file_path).suffix.casefold()
            if zone_folded in title and "регламент" in title:
                rank = 0
            elif "градостроительные регламенты" in title:
                rank = 1
            elif suffix == ".docx":
                rank = 2
            elif suffix in {".html", ".htm"}:
                rank = 3
            elif suffix == ".pdf":
                rank = 4
            else:
                rank = 5
            ranked.append((rank, file))
        ranked.sort(key=lambda item: (item[0], item[1].id))
        return [file for _, file in ranked]

    def extract_version(
        self,
        version_id: int,
        zone_codes: list[str],
        *,
        force: bool = False,
    ) -> list[ExtractionOutcome]:
        """Для каждой зоны — перебор файлов до первого успешного извлечения.

        Без force существующая extraction (version, zone, VRI_TABLE) не
        перезаписывается: возвращается кэшированный исход.
        """
        files = self.store.files_for_version(version_id)
        outcomes: list[ExtractionOutcome] = []
        for zone in zone_codes:
            cached = self.store.get_extraction(
                version_id, zone_code=zone, kind=ExtractionKind.VRI_TABLE
            )
            if cached is not None and not force:
                outcomes.append(self._cached_outcome(cached))
                continue
            outcome = self._extract_zone(version_id, files, zone)
            if outcome.status == "extracted" and outcome.table is not None:
                self._save(version_id, zone, outcome)
            outcomes.append(outcome)
        return outcomes

    def _extract_zone(
        self,
        version_id: int,
        files: list[VersionFileRecord],
        zone: str,
    ) -> ExtractionOutcome:
        last: ExtractionOutcome | None = None
        for file in self._candidates(files, zone):
            outcome = extract_vri_from_file(
                file.file_path, filename=file.title, zone_code=zone
            )
            last = outcome
            if outcome.status == "extracted":
                return outcome
        if last is None:
            return ExtractionOutcome(
                status="error",
                detail=f"у версии {version_id} нет файлов для извлечения",
                extractor="vri-dispatcher@1",
            )
        return last

    def _save(self, version_id: int, zone: str, outcome: ExtractionOutcome) -> None:
        table = outcome.table
        if table is None:
            return
        self.store.upsert_extraction(
            version_id,
            zone_code=zone,
            kind=ExtractionKind.VRI_TABLE,
            origin=ExtractionOrigin.FETCHED_FILE,
            payload=table.model_dump(),
            extractor=outcome.extractor or "unknown@1",
            confidence=table.confidence,
        )

    @staticmethod
    def _cached_outcome(record: ExtractionRecord) -> ExtractionOutcome:
        table = VriTable(**record.payload)
        return ExtractionOutcome(
            status="extracted",
            detail="already extracted",
            table=table,
            extractor=record.extractor,
        )
