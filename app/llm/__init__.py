"""LLM configuration: model catalog, OpenRouter-backed chat and embedding models."""

from app.llm.default_model import FALLBACK_MODEL, resolve_default_model
from app.llm.chat_model import build_chat_model, select_structured_method, structured
from app.llm.embedding_model import (
    EmbeddingBatchTooLargeError,
    EmbeddingClient,
    EmbeddingResult,
    EmptyEmbeddingBatchError,
    build_embedding_client,
    build_embedding_model,
)
from app.llm.models import (
    DEFAULT_MODEL,
    EnabledLLMModel,
    LLMModel,
    LLMProvider,
    ModelSpec,
    get_model_spec,
)

__all__ = [
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "resolve_default_model",
    "EmbeddingBatchTooLargeError",
    "EmbeddingClient",
    "EmbeddingResult",
    "EmptyEmbeddingBatchError",
    "EnabledLLMModel",
    "LLMModel",
    "LLMProvider",
    "ModelSpec",
    "build_chat_model",
    "build_embedding_client",
    "build_embedding_model",
    "get_model_spec",
    "select_structured_method",
    "structured",
]
