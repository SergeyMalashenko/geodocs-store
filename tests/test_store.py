"""Тесты DocumentStore на реальных примерах (ПЗЗ Солнечногорска)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from geodocs import (
    DocRole,
    DocType,
    DocumentRef,
    DocumentStore,
    ExtractionKind,
    ExtractionOrigin,
    FetchStatus,
    SourceName,
)

MUNICIPALITY = "Городской округ Солнечногорск"
BASE_NUMBER = "592"
BASE_DATE = "2021-04-21"
AMENDMENT_NUMBER = "944"
AMENDMENT_DATE = "2026-04-09"
CN_1 = "50:09:0000000:198995"
CN_2 = "50:09:0000000:198996"
ZONE = "Ж-2"


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[DocumentStore]:
    store = DocumentStore(tmp_path / "geodocs.sqlite3")
    yield store
    store.close()


def _base_ref(**overrides: Any) -> DocumentRef:
    data: dict[str, Any] = {
        "municipality": MUNICIPALITY,
        "doc_type": DocType.PZZ,
        "number": BASE_NUMBER,
        "version_date": AMENDMENT_DATE,
        "role": DocRole.AMENDMENT,
        "title": "Правила землепользования и застройки городского округа Солнечногорск",
        "issuer": "Совет депутатов городского округа Солнечногорск",
        "region_code": "50",
        "source": SourceName.RGIS,
        "source_object_id": "13881025700",
        "amendment_number": AMENDMENT_NUMBER,
    }
    data.update(overrides)
    return DocumentRef(**data)


def test_upsert_document_idempotent(store: DocumentStore) -> None:
    doc_id = store.upsert_document(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        region_code="50",
    )
    again = store.upsert_document(
        municipality=MUNICIPALITY, doc_type=DocType.PZZ, number=BASE_NUMBER
    )
    assert again == doc_id
    assert store.stats()["documents"] == 1


def test_upsert_version_idempotent(store: DocumentStore) -> None:
    doc_id = store.upsert_document(
        municipality=MUNICIPALITY, doc_type=DocType.PZZ, number=BASE_NUMBER
    )
    version_id = store.upsert_version(
        doc_id, version_date=BASE_DATE, role=DocRole.BASE
    )
    again = store.upsert_version(doc_id, version_date=BASE_DATE, role=DocRole.BASE)
    assert again == version_id
    other = store.upsert_version(
        doc_id,
        version_date=AMENDMENT_DATE,
        role=DocRole.AMENDMENT,
        amendment_number=AMENDMENT_NUMBER,
    )
    assert other != version_id
    assert store.stats()["document_versions"] == 2


def test_register_ref_end_to_end(store: DocumentStore) -> None:
    version_id = store.register_ref(_base_ref())
    again = store.register_ref(_base_ref())
    assert again == version_id

    stats = store.stats()
    assert stats["documents"] == 1
    assert stats["document_versions"] == 1
    assert stats["document_sources"] == 1

    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.role is DocRole.AMENDMENT
    assert version.amendment_number == AMENDMENT_NUMBER

    store.add_source(version_id, SourceName.NSPD, "50:09-7.17")
    store.add_source(version_id, SourceName.NSPD, "50:09-7.17")
    assert store.stats()["document_sources"] == 2


def test_read_helpers(store: DocumentStore) -> None:
    version_id = store.register_ref(_base_ref())
    store.add_source(version_id, SourceName.NSPD, "50:09-7.17")
    assert version_id is not None
    assert store.get_document(999999) is None

    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    document = store.get_document(version.document_id)
    assert document is not None
    assert document.municipality == MUNICIPALITY
    assert document.number == BASE_NUMBER
    assert document.doc_type is DocType.PZZ

    assert store.sources_for_version(version_id) == ["nspd", "rgis"]

    assert store.extractions_for_version(version_id) == []
    store.upsert_extraction(
        version_id,
        zone_code=ZONE,
        kind=ExtractionKind.VRI_TABLE,
        origin=ExtractionOrigin.FETCHED_FILE,
        payload={"main": ["жилой дом"]},
        extractor="pzz-text@v1",
    )
    (extraction,) = store.extractions_for_version(version_id)
    assert extraction.zone_code == ZONE
    assert extraction.payload == {"main": ["жилой дом"]}


def test_link_parcel_shared_version(store: DocumentStore) -> None:
    version_id = store.register_ref(_base_ref())
    store.link_parcel(CN_1, version_id)
    store.link_parcel(CN_2, version_id)
    assert store.stats()["parcel_documents"] == 2

    (link_1,) = store.links_for_version(version_id)[:1]
    time.sleep(0.01)
    store.link_parcel(CN_1, version_id)
    assert store.stats()["parcel_documents"] == 2

    links = {link.cadastral_number: link for link in store.links_for_version(version_id)}
    assert links[CN_1].last_seen_at > link_1.last_seen_at
    assert links[CN_1].first_seen_at == link_1.first_seen_at

    docs_1 = store.documents_for_parcel(CN_1)
    docs_2 = store.documents_for_parcel(CN_2)
    assert [doc.id for doc in docs_1] == [version_id]
    assert [doc.id for doc in docs_2] == [version_id]


def test_find_version_roundtrip(store: DocumentStore) -> None:
    assert (
        store.find_version(
            municipality=MUNICIPALITY,
            doc_type=DocType.PZZ,
            number=BASE_NUMBER,
            version_date=AMENDMENT_DATE,
        )
        is None
    )
    version_id = store.register_ref(_base_ref())
    version = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        version_date=AMENDMENT_DATE,
    )
    assert version is not None
    assert version.id == version_id
    assert version.fetch_status is FetchStatus.PENDING
    assert (
        store.find_version(
            municipality=MUNICIPALITY,
            doc_type=DocType.PZZ,
            number=BASE_NUMBER,
            version_date=BASE_DATE,
        )
        is None
    )


def test_save_file_dedup(store: DocumentStore, tmp_path: Path) -> None:
    version_a = store.register_ref(_base_ref())
    version_b = store.register_ref(
        _base_ref(version_date="2026-05-20", amendment_number="950")
    )

    content = b"%PDF-1.4 PZZ Solnechnogorsk N 592 fake body"
    path_a = store.save_file(
        version_a,
        content,
        filename="pzz_592_red_944.pdf",
        source_url="https://docs.cntd.ru/document/13881025700",
        source_provider=SourceName.RGIS,
    )
    assert path_a.exists()
    rec_a = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        version_date=AMENDMENT_DATE,
    )
    assert rec_a is not None
    assert rec_a.sha256 == hashlib.sha256(content).hexdigest()
    assert rec_a.fetch_status is FetchStatus.DOWNLOADED
    assert rec_a.fetched_at is not None
    assert rec_a.source_provider is SourceName.RGIS

    path_b = store.save_file(
        version_b, content, filename="copy.pdf", source_provider=SourceName.NSPD
    )
    assert path_b == path_a
    rec_b = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        version_date="2026-05-20",
    )
    assert rec_b is not None
    assert rec_b.file_path == str(path_a)
    # владельцем sha256 (и уникального индекса) остаётся первая версия
    assert rec_b.sha256 is None
    files = [p for p in (tmp_path / "files").rglob("*") if p.is_file()]
    assert len(files) == 1

    other = b"another revision body"
    path_c = store.save_file(
        version_b, other, filename="other.pdf", source_provider=SourceName.NSPD
    )
    assert path_c != path_a
    assert path_c.exists()
    rec_b = store.find_version(
        municipality=MUNICIPALITY,
        doc_type=DocType.PZZ,
        number=BASE_NUMBER,
        version_date="2026-05-20",
    )
    assert rec_b is not None
    assert rec_b.sha256 == hashlib.sha256(other).hexdigest()


def test_upsert_extraction_replaces_payload(store: DocumentStore) -> None:
    version_id = store.register_ref(_base_ref())
    ext_id = store.upsert_extraction(
        version_id,
        zone_code=ZONE,
        kind=ExtractionKind.VRI_TABLE,
        origin=ExtractionOrigin.FETCHED_FILE,
        payload={"main": ["жилой дом"]},
        extractor="pzz-text@v1",
        confidence=0.9,
    )
    again = store.upsert_extraction(
        version_id,
        zone_code=ZONE,
        kind=ExtractionKind.VRI_TABLE,
        origin=ExtractionOrigin.FETCHED_FILE,
        payload={"main": ["жилой дом", "садовый дом"]},
        extractor="pzz-text@v2",
        confidence=0.95,
    )
    assert again == ext_id
    assert store.stats()["extractions"] == 1

    record = store.get_extraction(
        version_id, zone_code=ZONE, kind=ExtractionKind.VRI_TABLE
    )
    assert record is not None
    assert record.payload == {"main": ["жилой дом", "садовый дом"]}
    assert record.extractor == "pzz-text@v2"
    assert record.confidence == 0.95
    assert record.origin is ExtractionOrigin.FETCHED_FILE
    assert (
        store.get_extraction(
            version_id, zone_code=ZONE, kind=ExtractionKind.HEIGHT_LIMITS
        )
        is None
    )


def test_wal_enabled(store: DocumentStore) -> None:
    mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


def test_concurrent_stores(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.sqlite3"
    first = DocumentStore(db_path)
    second = DocumentStore(db_path)
    try:
        version_1 = first.register_ref(_base_ref())
        version_2 = second.register_ref(_base_ref())
        assert version_1 == version_2

        second.link_parcel(CN_1, version_1)
        first.link_parcel(CN_2, version_2)
        first.set_fetch_status(version_1, FetchStatus.SEARCH_FAILED)

        assert first.stats()["parcel_documents"] == 2
        assert second.stats()["documents"] == 1
        version = second.find_version(
            municipality=MUNICIPALITY,
            doc_type=DocType.PZZ,
            number=BASE_NUMBER,
            version_date=AMENDMENT_DATE,
        )
        assert version is not None
        assert version.fetch_status is FetchStatus.SEARCH_FAILED
    finally:
        first.close()
        second.close()


def test_stats(store: DocumentStore) -> None:
    assert store.stats() == {
        "documents": 0,
        "document_versions": 0,
        "document_sources": 0,
        "version_files": 0,
        "parcel_documents": 0,
        "extractions": 0,
    }
    version_id = store.register_ref(_base_ref())
    store.link_parcel(CN_1, version_id)
    store.upsert_extraction(
        version_id,
        zone_code=ZONE,
        kind=ExtractionKind.ZONE_DESCRIPTION,
        origin=ExtractionOrigin.PROVIDER_ATTRIBUTES,
        payload={"text": "Зона многоквартирной жилой застройки"},
        extractor="rgis-attributes@v1",
    )
    assert store.stats() == {
        "documents": 1,
        "document_versions": 1,
        "document_sources": 1,
        "version_files": 0,
        "parcel_documents": 1,
        "extractions": 1,
    }


def test_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEODOCS_HOME", str(tmp_path / "home"))
    with DocumentStore.from_env() as store:
        assert store.db_path == tmp_path / "home" / "geodocs.sqlite3"
        assert store.files_dir == tmp_path / "home" / "files"
        assert store.db_path.exists()


def test_store_reconnects_after_close(tmp_path):
    """close() не должен убивать store: MCP-серверы переоткрывают соединение."""
    store = DocumentStore(tmp_path / "geodocs.sqlite3")
    ref = DocumentRef(
        municipality="Городской округ Клин",
        doc_type=DocType.PZZ,
        number="1756",
        version_date="2026-05-25",
        role=DocRole.SINGLE,
        region_code="50",
        source=SourceName.RGIS,
        source_object_id="1",
    )
    version_id = store.register_ref(ref)
    store.close()

    store.link_parcel("50:03:0070212:522", version_id)
    (versions,) = [store.documents_for_parcel("50:03:0070212:522")]
    assert [v.id for v in versions] == [version_id]
    store.close()
