import uuid

import openai
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agents import (
    AgentContext,
    KnowledgeItem,
    KnowledgeItemQueries,
    KnowledgeQueryAgent,
    KnowledgeQueryGenerationError,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.usage import set_usage_scope


router = APIRouter(tags=["knowledge"])


class KnowledgeQueriesRequest(BaseModel):
    items: list[KnowledgeItem] = Field(min_length=1)
    model: LLMModel = DEFAULT_MODEL
    # What this call's LLM spend is booked against. Optional so an Orchestration
    # that does not send it yet keeps working, with a null reference until then.
    project_id: int | None = None


class KnowledgeQueriesResponse(BaseModel):
    results: list[KnowledgeItemQueries]


def _agent(app) -> KnowledgeQueryAgent:
    return app.state.knowledge_query_agent


@router.post("/knowledge-queries", response_model=KnowledgeQueriesResponse)
async def generate_knowledge_queries(
    payload: KnowledgeQueriesRequest, request: Request
) -> KnowledgeQueriesResponse:
    limit = request.app.state.knowledge_query_batch_limit
    if len(payload.items) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"{len(payload.items)} items exceeds the limit of {limit} per call.",
        )

    # Set before the fan-out: run_batch spawns one model call per item, and the
    # contextvar is copied into each of those tasks.
    set_usage_scope("KNOWLEDGE_QUERY", payload.project_id)
    # session_id is correlation-only; query generation is stateless.
    context = AgentContext(session_id=f"knowledge-queries-{uuid.uuid4().hex}")
    try:
        results = await _agent(request.app).run_batch(
            payload.items, context, payload.model
        )
    except KnowledgeQueryGenerationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except openai.APIError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return KnowledgeQueriesResponse(results=results)
