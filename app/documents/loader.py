"""File bytes → normalized text for game_context extraction.

A ``DocumentLoader`` is a swappable strategy: v1 ships text extraction
(pdfplumber for PDF, decode for txt/md). A future multimodal strategy can slot
in behind the same ``bytes -> str`` contract without touching the agent.

Fetching bytes (from an upload body or an S3 URL) is the ingestion service's
job, not the loader's — loaders stay pure and take raw bytes so they are trivial
to unit-test.
"""

import io
from typing import Protocol

import pdfplumber


class UnsupportedDocumentError(RuntimeError):
    """Raised when no loader is registered for a document kind."""


class DocumentLoader(Protocol):
    def extract_text(self, data: bytes) -> str:
        """Return normalized plain text extracted from the document bytes."""


class PdfLoader:
    """PDF text via pdfplumber — preserves line/list structure noticeably better
    than pypdf, which matters for extracting ordered flows (e.g. tutorials)."""

    def extract_text(self, data: bytes) -> str:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [page.extract_text() or "" for page in pdf.pages]
        return "\n".join(pages).strip()


class TextLoader:
    """Plain text / markdown — decode passthrough."""

    def extract_text(self, data: bytes) -> str:
        return data.decode("utf-8", errors="replace").strip()


# Document kind → loader. Kept small and explicit; extend as formats are added.
_LOADERS: dict[str, DocumentLoader] = {
    "pdf": PdfLoader(),
    "text": TextLoader(),
}

_EXTENSION_KIND: dict[str, str] = {
    ".pdf": "pdf",
    ".txt": "text",
    ".md": "text",
    ".markdown": "text",
}

_CONTENT_TYPE_KIND: dict[str, str] = {
    "application/pdf": "pdf",
    "text/plain": "text",
    "text/markdown": "text",
}


def detect_kind(
    *, filename: str | None = None, content_type: str | None = None
) -> str:
    """Resolve a document kind from filename extension or MIME type.

    Extension wins when present; content type is the fallback. Raises
    ``UnsupportedDocumentError`` when neither identifies a supported kind.
    """
    if filename:
        _, _, ext = filename.lower().rpartition(".")
        kind = _EXTENSION_KIND.get(f".{ext}") if ext else None
        if kind:
            return kind
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        kind = _CONTENT_TYPE_KIND.get(base)
        if kind:
            return kind
    raise UnsupportedDocumentError(
        f"Unsupported document (filename={filename!r}, content_type={content_type!r})."
    )


def extract_document_text(
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
) -> str:
    """Detect the document kind and extract normalized text from its bytes."""
    loader = _LOADERS[detect_kind(filename=filename, content_type=content_type)]
    return loader.extract_text(data)
