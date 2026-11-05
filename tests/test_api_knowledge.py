"""The two knowledge-indexing endpoints, wired over canned agents/clients.

Both are stateless: they produce vectors and search queries and hand them back.
Nothing here writes anything down — the store is Orchestration's.
"""

import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from langchain_core.runnables import RunnableLambda

from app.agents import KnowledgeQueryAgent
from app.agents.knowledge_query.schemas import KnowledgeQueries
from app.api.embeddings import router as embeddings_router
from app.api.knowledge_queries import router as knowledge_queries_router
from app.llm.embedding_model import EmbeddingClient


class _FixedEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FailingEmbeddings(Embeddings):
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise openai.APIError("upstream down", request=None, body=None)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _embed_client(embeddings: Embeddings, batch_limit: int = 3) -> TestClient:
    app = FastAPI()
    app.include_router(embeddings_router)
    app.state.embedding_client = EmbeddingClient(
        model="openai/text-embedding-3-large",
        dimensions=3,
        batch_limit=batch_limit,
        embeddings=embeddings,
    )
    return TestClient(app)


def _queries_client(agent: KnowledgeQueryAgent, batch_limit: int = 2) -> TestClient:
    app = FastAPI()
    app.include_router(knowledge_queries_router)
    app.state.knowledge_query_agent = agent
    app.state.knowledge_query_batch_limit = batch_limit
    return TestClient(app)


# --- POST /embed --------------------------------------------------------------


def test_embed_returns_a_vector_per_text_and_names_the_model() -> None:
    client = _embed_client(_FixedEmbeddings())

    response = client.post("/embed", json={"texts": ["첫 문장", "둘째 문장"]})

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "openai/text-embedding-3-large"
    assert body["dimensions"] == 3
    assert body["vectors"] == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]


def test_embed_rejects_more_texts_than_the_limit() -> None:
    client = _embed_client(_FixedEmbeddings(), batch_limit=2)

    response = client.post("/embed", json={"texts": ["a", "b", "c"]})

    assert response.status_code == 422
    assert "limit of 2" in response.json()["detail"]


def test_embed_rejects_an_empty_array() -> None:
    response = _embed_client(_FixedEmbeddings()).post("/embed", json={"texts": []})

    assert response.status_code == 422


def test_embed_reports_an_upstream_failure_as_a_gateway_error() -> None:
    response = _embed_client(_FailingEmbeddings()).post("/embed", json={"texts": ["a"]})

    assert response.status_code == 502


# --- POST /knowledge-queries --------------------------------------------------


def _agent_returning(queries: list[str]) -> KnowledgeQueryAgent:
    return KnowledgeQueryAgent(
        structured_factory=lambda model: RunnableLambda(
            lambda _inputs: KnowledgeQueries(queries=queries)
        )
    )


def test_knowledge_queries_answers_per_item() -> None:
    agent = _agent_returning(["골드 없으면?", "구매 조건", "소지금 부족 시"])
    client = _queries_client(agent)

    response = client.post(
        "/knowledge-queries",
        json={
            "items": [
                {"id": "k-1", "summary": "골드 부족 시 구매 불가", "description": "..."},
                {"id": "k-2", "summary": "인벤토리 정원", "description": "..."},
            ]
        },
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["id"] for result in results] == ["k-1", "k-2"]
    assert len(results[0]["queries"]) == 3


def test_knowledge_queries_accepts_an_item_without_a_description() -> None:
    client = _queries_client(_agent_returning(["질문 하나"]))

    response = client.post(
        "/knowledge-queries", json={"items": [{"id": "k-1", "summary": "길드 정원"}]}
    )

    assert response.status_code == 200


def test_knowledge_queries_rejects_more_items_than_the_limit() -> None:
    client = _queries_client(_agent_returning(["질문"]), batch_limit=1)

    response = client.post(
        "/knowledge-queries",
        json={"items": [{"id": "a", "summary": "s"}, {"id": "b", "summary": "s"}]},
    )

    assert response.status_code == 422
    assert "limit of 1" in response.json()["detail"]


def test_knowledge_queries_rejects_an_empty_array() -> None:
    response = _queries_client(_agent_returning(["질문"])).post(
        "/knowledge-queries", json={"items": []}
    )

    assert response.status_code == 422


def test_knowledge_queries_surfaces_an_unusable_generation_as_422() -> None:
    client = _queries_client(_agent_returning(["  "]))

    response = client.post(
        "/knowledge-queries", json={"items": [{"id": "k-1", "summary": "s"}]}
    )

    assert response.status_code == 422


def test_both_endpoints_are_published_in_the_contract() -> None:
    from app.main import app

    schema = TestClient(app).get("/openapi.json").json()

    assert "/internal/embed" in schema["paths"]
    assert "/internal/knowledge-queries" in schema["paths"]
    assert schema["paths"]["/internal/embed"]["post"]["tags"] == ["knowledge"]
