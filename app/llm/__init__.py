"""LLM configuration: model catalog, chat and embedding models.

`build_chat_model` serves OpenRouter by default and, under
`Settings.llm_backend == "claude_subscription"`, a local-only backend defined
in `app.llm.claude_subscription`. That module is not imported here — it pulls
in `claude-agent-sdk`, a dev-only dependency, so `app.llm.chat_model` imports it
lazily inside the function instead of at module load time.
"""

from app.llm.chat_model import build_chat_model, select_structured_method
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
    MODEL_SPECS,
    LLMModel,
    LLMProvider,
    ModelSpec,
    get_model_spec,
    list_models,
)

__all__ = [
    "DEFAULT_MODEL",
    "MODEL_SPECS",
    "EmbeddingBatchTooLargeError",
    "EmbeddingClient",
    "EmbeddingResult",
    "EmptyEmbeddingBatchError",
    "LLMModel",
    "LLMProvider",
    "ModelSpec",
    "build_chat_model",
    "build_embedding_client",
    "build_embedding_model",
    "get_model_spec",
    "list_models",
    "select_structured_method",
]
