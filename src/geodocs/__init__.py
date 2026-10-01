"""geodocs — общее хранилище градостроительных документов."""

from .extractors import (
    ExtractionOutcome,
    ExtractionStatus,
    VriExtractor,
    VriItem,
    VriTable,
    demap,
    extract_html,
    extract_vri_from_file,
    looks_like_mojibake,
)
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
    ParcelDocumentLink,
    SourceName,
    VersionFileRecord,
)
from .store import DocumentStore

__version__ = "0.2.0"

__all__ = [
    "DocRole",
    "DocType",
    "DocumentRecord",
    "DocumentRef",
    "DocumentStore",
    "DocumentVersionRecord",
    "ExtractionKind",
    "ExtractionOrigin",
    "ExtractionOutcome",
    "ExtractionRecord",
    "ExtractionStatus",
    "FetchStatus",
    "ParcelDocumentLink",
    "SourceName",
    "VersionFileRecord",
    "VriExtractor",
    "VriItem",
    "VriTable",
    "__version__",
    "demap",
    "extract_html",
    "extract_vri_from_file",
    "looks_like_mojibake",
]
