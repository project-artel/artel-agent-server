import pytest

from app.documents import loader as loader_module
from app.documents import (
    PdfLoader,
    TextLoader,
    UnsupportedDocumentError,
    detect_kind,
    extract_document_text,
)


def test_text_loader_decodes_and_strips() -> None:
    assert TextLoader().extract_text(b"  hello \xea\xb2\x8c\xec\x9e\x84  ") == "hello 게임"


def test_text_loader_replaces_invalid_bytes() -> None:
    # Invalid UTF-8 must not raise (errors="replace").
    assert "�" in TextLoader().extract_text(b"\xff\xfe bad")


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("design.pdf", None, "pdf"),
        ("notes.TXT", None, "text"),
        ("readme.md", None, "text"),
        (None, "application/pdf", "pdf"),
        (None, "text/markdown; charset=utf-8", "text"),
        ("no-extension", "application/pdf", "pdf"),  # extension miss → content type
    ],
)
def test_detect_kind(filename, content_type, expected) -> None:
    assert detect_kind(filename=filename, content_type=content_type) == expected


def test_detect_kind_unsupported_raises() -> None:
    with pytest.raises(UnsupportedDocumentError):
        detect_kind(filename="archive.zip", content_type="application/zip")


def test_detect_kind_requires_a_hint() -> None:
    with pytest.raises(UnsupportedDocumentError):
        detect_kind()


class _FakePage:
    def __init__(self, text: str | None) -> None:
        self._text = text

    def extract_text(self) -> str | None:
        return self._text


class _FakePdf:
    def __init__(self, pages) -> None:
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


def test_pdf_loader_joins_pages_and_tolerates_empty(monkeypatch) -> None:
    pages = [_FakePage("page one"), _FakePage(None), _FakePage("page three")]
    monkeypatch.setattr(loader_module.pdfplumber, "open", lambda _stream: _FakePdf(pages))

    assert PdfLoader().extract_text(b"%PDF-fake") == "page one\n\npage three"


def test_extract_document_text_dispatches_by_kind(monkeypatch) -> None:
    pages = [_FakePage("combat rules")]
    monkeypatch.setattr(loader_module.pdfplumber, "open", lambda _stream: _FakePdf(pages))

    assert extract_document_text(b"%PDF", filename="g.pdf") == "combat rules"
    assert extract_document_text(b"plain notes", filename="g.txt") == "plain notes"
