"""Document ingestion: file bytes → normalized text for extraction."""

from app.documents.loader import (
    DocumentLoader,
    PdfLoader,
    TextLoader,
    UnsupportedDocumentError,
    detect_kind,
    extract_document_text,
)

__all__ = [
    "DocumentLoader",
    "PdfLoader",
    "TextLoader",
    "UnsupportedDocumentError",
    "detect_kind",
    "extract_document_text",
]
