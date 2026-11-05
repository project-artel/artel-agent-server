import asyncio

import pytest
from langchain_core.embeddings import Embeddings

from app.config import Settings
from app.llm.embedding_model import (
    EmbeddingBatchTooLargeError,
    EmbeddingClient,
    EmptyEmbeddingBatchError,
    build_embedding_client,
    build_embedding_model,
)


class RecordingEmbeddings(Embeddings):
    """Captures the batch it was handed and returns fixed-width vectors."""

    def __init__(self, dimensions: int = 4) -> None:
        self.calls: list[list[str]] = []
        self._dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(index)] * self._dimensions for index, _ in enumerate(texts)]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _client(embeddings: Embeddings, batch_limit: int = 3) -> EmbeddingClient:
    return EmbeddingClient(
        model="openai/text-embedding-3-large",
        dimensions=4,
        batch_limit=batch_limit,
        embeddings=embeddings,
    )


def test_embed_sends_the_whole_batch_in_one_call() -> None:
    embeddings = RecordingEmbeddings()
    client = _client(embeddings)

    result = asyncio.run(client.embed(["첫 문장", "둘째 문장", "셋째 문장"]))

    # One call with three texts, not three calls with one — the point of the
    # array input is that a backfill worker pays one round trip.
    assert embeddings.calls == [["첫 문장", "둘째 문장", "셋째 문장"]]
    assert len(result.vectors) == 3


def test_embed_reports_the_model_that_produced_the_vectors() -> None:
    result = asyncio.run(_client(RecordingEmbeddings()).embed(["문장"]))

    assert result.model == "openai/text-embedding-3-large"
    assert result.dimensions == 4


def test_embed_preserves_input_order() -> None:
    result = asyncio.run(_client(RecordingEmbeddings()).embed(["a", "b", "c"]))

    assert [vector[0] for vector in result.vectors] == [0.0, 1.0, 2.0]


def test_embed_rejects_a_batch_over_the_limit() -> None:
    embeddings = RecordingEmbeddings()
    client = _client(embeddings, batch_limit=2)

    with pytest.raises(EmbeddingBatchTooLargeError, match="limit of 2"):
        asyncio.run(client.embed(["a", "b", "c"]))

    assert embeddings.calls == []  # rejected before the endpoint is touched


def test_embed_accepts_a_batch_exactly_at_the_limit() -> None:
    client = _client(RecordingEmbeddings(), batch_limit=2)

    result = asyncio.run(client.embed(["a", "b"]))

    assert len(result.vectors) == 2


def test_embed_rejects_an_empty_batch() -> None:
    with pytest.raises(EmptyEmbeddingBatchError):
        asyncio.run(_client(RecordingEmbeddings()).embed([]))


def test_build_embedding_client_takes_the_slug_and_width_from_settings() -> None:
    settings = Settings(
        embedding_model="qwen/qwen3-embedding-8b",
        embedding_dimensions=1024,
        embedding_batch_limit=64,
    )

    client = build_embedding_client(settings)

    assert client.model == "qwen/qwen3-embedding-8b"
    assert client.batch_limit == 64


def test_the_underlying_model_points_at_openrouter_and_asks_for_the_width() -> None:
    model = build_embedding_model("openai/text-embedding-3-large", 1024, 128)

    assert model.model == "openai/text-embedding-3-large"
    assert model.dimensions == 1024
    assert "openrouter.ai" in model.openai_api_base
    # Tiktoken-based splitting is off, so texts reach the endpoint verbatim and
    # a permitted batch is a single request.
    assert model.check_embedding_ctx_length is False
    assert model.chunk_size == 128
