from fastapi import APIRouter
from pydantic import BaseModel

from app.llm.models import LLMProvider, ReasoningEffort, ReasoningKind, list_models


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
