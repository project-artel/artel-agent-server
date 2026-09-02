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
    # 이 모델을 고르면 지식창고 검색이 꺼진다. 화면이 그 사실을 미리 말해 줄 수 있도록
    # 카탈로그에 싣는다 — 런이 끝난 뒤 "왜 하나도 안 찾았지"를 묻게 두지 않는다.
    knowledge_search: bool
    input_modalities: list[str]
    multimodal: bool
    reasoning: ReasoningCapability | None


@router.get("/models", response_model=list[ModelCatalogEntry])
async def models() -> list[dict]:
    return list_models()
