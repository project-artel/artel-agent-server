"""LLM configuration: model catalog and OpenRouter-backed chat models."""

from app.llm.chat_model import build_chat_model, select_structured_method
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
    "LLMModel",
    "LLMProvider",
    "ModelSpec",
    "build_chat_model",
    "get_model_spec",
    "list_models",
    "select_structured_method",
]
