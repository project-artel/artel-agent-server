"""Fetch document bytes from a (presigned) URL for extraction.

Kept separate from the loader so the loader stays a pure ``bytes -> text``
transform. Guards: scheme/host validation, no redirects (SSRF), streamed size
cap, and a timeout.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class DocumentFetchError(RuntimeError):
    """Raised when a source URL cannot be fetched or fails a guard."""


@dataclass(frozen=True)
class FetchedDocument:
    data: bytes
    content_type: str | None


def _validate_url(url: str, allowed_hosts: Sequence[str]) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise DocumentFetchError(f"Only http(s) URLs are allowed, got {parts.scheme!r}.")
    if allowed_hosts and parts.hostname not in allowed_hosts:
        raise DocumentFetchError(f"Host not allowed: {parts.hostname!r}.")


async def fetch_document(
    url: str,
    *,
    client: httpx.AsyncClient,
    max_bytes: int,
    timeout: float,
    allowed_hosts: Sequence[str] = (),
) -> FetchedDocument:
    """Stream the URL into memory, aborting past ``max_bytes``.

    Redirects are disabled so a presigned URL cannot bounce the fetch to an
    internal address.
    """
    _validate_url(url, allowed_hosts)
    try:
        async with client.stream(
            "GET", url, timeout=timeout, follow_redirects=False
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type")
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise DocumentFetchError(
                        f"Document exceeds max size ({max_bytes} bytes)."
                    )
                chunks.append(chunk)
    except httpx.HTTPError as error:
        raise DocumentFetchError(f"Failed to fetch source URL: {error}") from error
    return FetchedDocument(data=b"".join(chunks), content_type=content_type)
