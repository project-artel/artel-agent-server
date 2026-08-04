"""Text → vectors, over the same OpenRouter credential the chat models use.

The mirror of ``chat_model.py``: OpenRouter's ``/v1/embeddings`` is the OpenAI
shape, so the OpenAI client class reaches the whole embedding catalog through
the model slug and nothing new has to be configured to talk to it.

Nothing here is stored. The agent server is the only place that holds a key
capable of producing vectors; Orchestration owns where they live. That is why
``EmbeddingResult`` carries the model slug and width out with the vectors — the
side that writes them has to know which model made a row before it can decide
whether a re-index is due.
"""

from dataclasses import dataclass
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config import Settings, get_settings
from app.llm.usage import build_usage_http_client


class EmbeddingBatchTooLargeError(ValueError):
    """More texts in one call than the configured batch limit allows."""


class EmptyEmbeddingBatchError(ValueError):
    """An embedding call arrived with nothing to embed."""


@dataclass(frozen=True)
class EmbeddingResult:
    model: str
    dimensions: int
    vectors: list[list[float]]


@lru_cache
def build_embedding_model(
    model: str, dimensions: int, batch_limit: int
) -> OpenAIEmbeddings:
    """Build an embedding client for an OpenRouter slug."""
    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_title:
        headers["X-Title"] = settings.openrouter_app_title

    return OpenAIEmbeddings(
        model=model,
        dimensions=dimensions,
        openai_api_base=settings.openrouter_base_url,
        openai_api_key=settings.openrouter_api_key or "missing",
        default_headers=headers or None,
        # Left on, LangChain re-encodes every input with tiktoken, splits it at
        # OpenAI's context length and averages the pieces back into one vector.
        # That repair assumes OpenAI's tokenizer, and OpenRouter serves slugs
        # from every vendor — the cuts would land in the wrong places and the
        # vector handed back would not be the one the model produced. Off, the
        # texts reach the endpoint verbatim.
        check_embedding_ctx_length=False,
        # LangChain still slices a batch into chunks of this size, one request
        # each. Matching the caller's own limit makes a permitted batch exactly
        # one request rather than however many the library default implies.
        chunk_size=batch_limit,
        # Embeddings have no LangChain callback to hang usage off — they are not
        # a chat model and never enter an agent graph — so the token counts are
        # read off the HTTP response instead. This function is @lru_cache'd, so
        # the client (and its connection pool) is built once.
        http_async_client=build_usage_http_client(),
    )


class EmbeddingClient:
    """Embeds a batch of texts and reports which model produced the vectors.

    ``embeddings`` is injectable so tests can supply a canned implementation
    instead of calling a real endpoint.
    """

    def __init__(
        self,
        model: str,
        dimensions: int,
        batch_limit: int,
        embeddings: Embeddings | None = None,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._batch_limit = batch_limit
        self._embeddings = embeddings or build_embedding_model(
            model, dimensions, batch_limit
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def batch_limit(self) -> int:
        return self._batch_limit

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            raise EmptyEmbeddingBatchError("Provide at least one text to embed.")
        if len(texts) > self._batch_limit:
            raise EmbeddingBatchTooLargeError(
                f"{len(texts)} texts exceeds the limit of {self._batch_limit} per call."
            )

        vectors = await self._embeddings.aembed_documents(texts)
        return EmbeddingResult(
            model=self._model,
            dimensions=self._dimensions,
            vectors=vectors,
        )


def build_embedding_client(settings: Settings | None = None) -> EmbeddingClient:
    resolved = settings or get_settings()
    return EmbeddingClient(
        model=resolved.embedding_model,
        dimensions=resolved.embedding_dimensions,
        batch_limit=resolved.embedding_batch_limit,
    )
