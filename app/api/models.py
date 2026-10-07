from fastapi import APIRouter
from pydantic import BaseModel

from app.config import get_settings
from app.llm.models import (
    LLMProvider,
    ReasoningEffort,
    ReasoningKind,
    list_models,
    required_openrouter_slugs,
)


router = APIRouter(tags=["models"])


class ReasoningCapability(BaseModel):
    kind: ReasoningKind
    efforts: list[ReasoningEffort] | None = None
    min_tokens: int | None = None
    max_tokens: int | None = None
    step: int | None = None


class ModelCatalogEntry(BaseModel):
    id: str
    label: str
    provider: LLMProvider
    supports_strict_json: bool
    supports_vision: bool
    input_modalities: list[str]
    multimodal: bool
    reasoning: ReasoningCapability | None


@router.get("/models", response_model=list[ModelCatalogEntry])
async def models() -> list[dict]:
    return list_models()


class RequiredModels(BaseModel):
    slugs: list[str]


@router.get("/models/required", response_model=RequiredModels)
async def required_models() -> RequiredModels:
    """OpenRouter slugs the configured API key must reach: chat catalog and embeddings."""
    return RequiredModels(slugs=required_openrouter_slugs(get_settings().embedding_model))
