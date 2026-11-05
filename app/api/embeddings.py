import openai
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.llm.embedding_model import (
    EmbeddingBatchTooLargeError,
    EmbeddingClient,
    EmptyEmbeddingBatchError,
)
from app.llm.usage import set_usage_scope


router = APIRouter(tags=["knowledge"])


class EmbedRequest(BaseModel):
    # A list, not a single string: the caller is a backfill worker that always
    # has many rows in hand, and OpenRouter embeds a batch in one round trip.
    texts: list[str] = Field(min_length=1)
    # What this call's LLM spend is booked against. Optional so an Orchestration
    # that does not send it yet keeps working, with a null reference until then.
    project_id: int | None = None


class EmbedResponse(BaseModel):
    # The slug travels with the vectors so the side that stores them can tell
    # which model a row came from and decide when a re-index is due.
    model: str
    dimensions: int
    vectors: list[list[float]]


def _client(app) -> EmbeddingClient:
    return app.state.embedding_client


@router.post("/embed", response_model=EmbedResponse)
async def embed(payload: EmbedRequest, request: Request) -> EmbedResponse:
    set_usage_scope("EMBEDDING", payload.project_id)
    try:
        result = await _client(request.app).embed(payload.texts)
    except (EmbeddingBatchTooLargeError, EmptyEmbeddingBatchError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except openai.APIError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return EmbedResponse(
        model=result.model,
        dimensions=result.dimensions,
        vectors=result.vectors,
    )
