"""Document ingestion: fetch → normalized text → extraction service."""

from app.documents.fetch import (
    DocumentFetchError,
    FetchedDocument,
    fetch_document,
)
from app.documents.loader import (
    DocumentLoader,
    PdfLoader,
    TextLoader,
    UnsupportedDocumentError,
    detect_kind,
    extract_document_text,
)
from app.documents.service import ExtractionService

__all__ = [
    "DocumentFetchError",
    "DocumentLoader",
    "ExtractionService",
    "FetchedDocument",
    "PdfLoader",
    "TextLoader",
    "UnsupportedDocumentError",
    "detect_kind",
    "extract_document_text",
    "fetch_document",
]
