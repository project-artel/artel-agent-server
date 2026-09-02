import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents import GameContext, GameContextAgent
from app.agents.game_context.schemas import Overview
from app.api.extract import router as extract_router
from app.documents import DocumentFetchError, ExtractionService, fetch_document


def _client_returning(body: bytes, *, content_type: str = "text/plain") -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- fetch_document -----------------------------------------------------------


def test_fetch_document_returns_bytes_and_content_type() -> None:
    async def run():
        async with _client_returning(b"hello", content_type="text/markdown") as client:
            return await fetch_document(
                "https://bucket.s3.amazonaws.com/x", client=client, max_bytes=100, timeout=5
            )

    fetched = asyncio.run(run())
    assert fetched.data == b"hello"
    assert fetched.content_type == "text/markdown"


def test_fetch_document_enforces_size_cap() -> None:
    async def run():
        async with _client_returning(b"x" * 50) as client:
            await fetch_document(
                "https://bucket.s3.amazonaws.com/x", client=client, max_bytes=10, timeout=5
            )

    with pytest.raises(DocumentFetchError):
        asyncio.run(run())


def test_fetch_document_rejects_non_http_scheme() -> None:
    async def run():
        async with _client_returning(b"x") as client:
            await fetch_document("file:///etc/passwd", client=client, max_bytes=10, timeout=5)

    with pytest.raises(DocumentFetchError):
        asyncio.run(run())


def test_fetch_document_enforces_host_allowlist() -> None:
    async def run():
        async with _client_returning(b"x") as client:
            await fetch_document(
                "https://evil.example.com/x",
                client=client,
                max_bytes=10,
                timeout=5,
                allowed_hosts=["bucket.s3.amazonaws.com"],
            )

    with pytest.raises(DocumentFetchError):
        asyncio.run(run())


# --- /extract route -----------------------------------------------------------


def _canned_agent(result: GameContext) -> GameContextAgent:
    return GameContextAgent(structured_factory=lambda model: RunnableLambda(lambda _i: result))


def _failing_agent() -> GameContextAgent:
    def boom(_i):
        raise OutputParserException("bad json")

    return GameContextAgent(structured_factory=lambda model: RunnableLambda(boom))


def _app_with_service(service: ExtractionService) -> FastAPI:
    app = FastAPI()
    app.state.extraction_service = service
    app.include_router(extract_router)
    return app


def test_extract_route_returns_game_context() -> None:
    # genre 를 함께 채운다 — title 만 있으면 description 이 전부 비어 항목이
    # 걸러지고, 아래 단언이 빈 리스트를 통과시키는 공허한 검증이 된다.
    result = GameContext(overview=Overview(title="WordVenture", genre="word puzzle"))
    service = ExtractionService(
        agent=_canned_agent(result),
        http_client=_client_returning(b"a design doc body"),
        max_bytes=1000,
        timeout=5,
    )
    client = TestClient(_app_with_service(service))

    resp = client.post(
        "/extract",
        json={"source_url": "https://bucket.s3.amazonaws.com/g.txt", "filename": "g.txt"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "g.txt"
    # orchestration-server 의 AgentExtractClient 가 읽는 그대로: tag/summary/
    # description 세 필드만 가진 항목의 배열 (ARTEL-745).
    assert body["game_context"] == [
        {"tag": "OBJECTIVE", "summary": "WordVenture", "description": "genre: word puzzle"}
    ]


def test_extract_route_maps_extraction_failure_to_422() -> None:
    service = ExtractionService(
        agent=_failing_agent(),
        http_client=_client_returning(b"body"),
        max_bytes=1000,
        timeout=5,
    )
    client = TestClient(_app_with_service(service))

    resp = client.post(
        "/extract",
        json={"source_url": "https://bucket.s3.amazonaws.com/g.txt", "filename": "g.txt"},
    )

    assert resp.status_code == 422


def test_extract_route_maps_unsupported_document_to_415() -> None:
    # Unsupported by BOTH extension and content-type (the loader falls back to
    # content-type when the extension is unknown).
    service = ExtractionService(
        agent=_canned_agent(GameContext()),
        http_client=_client_returning(b"body", content_type="application/zip"),
        max_bytes=1000,
        timeout=5,
    )
    client = TestClient(_app_with_service(service))

    resp = client.post(
        "/extract",
        json={"source_url": "https://bucket.s3.amazonaws.com/x.zip", "filename": "x.zip"},
    )

    assert resp.status_code == 415
