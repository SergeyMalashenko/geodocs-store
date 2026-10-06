"""geodocs — общее хранилище градостроительных документов."""

from .agent.query import Evidence, QueryStatus
from .api import (
    AcquireResult,
    AcquireStatus,
    QueryResult,
    acquire_documents,
    query_documents,
)
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
    FileRecord,
    ParcelDocumentLink,
    SourceName,
    VersionFileRecord,
)
from .store import DocumentStore

__version__ = "0.2.0"

__all__ = [
    "AcquireResult",
    "AcquireStatus",
    "DocRole",
    "DocType",
    "DocumentRecord",
    "DocumentRef",
    "DocumentStore",
    "DocumentVersionRecord",
    "Evidence",
    "ExtractionKind",
    "ExtractionOrigin",
    "ExtractionOutcome",
    "ExtractionRecord",
    "ExtractionStatus",
    "FetchStatus",
    "FileRecord",
    "ParcelDocumentLink",
    "QueryResult",
    "QueryStatus",
    "SourceName",
    "VersionFileRecord",
    "VriExtractor",
    "VriItem",
    "VriTable",
    "__version__",
    "acquire_documents",
    "demap",
    "extract_html",
    "extract_vri_from_file",
    "looks_like_mojibake",
    "query_documents",
]
