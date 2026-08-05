import openai
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.agents import GameContext, GameContextExtractionError
from app.documents.fetch import DocumentFetchError
from app.documents.loader import UnsupportedDocumentError
from app.documents.service import ExtractionService
from app.llm.models import DEFAULT_MODEL, LLMModel


router = APIRouter(tags=["game_context"])


class ExtractRequest(BaseModel):
    source_url: str
    filename: str
    model: LLMModel = DEFAULT_MODEL
    # What this call's LLM spend is booked against. Optional so an Orchestration
    # that does not send it yet keeps working, with a null reference until then.
    document_id: int | None = None


class ExtractResponse(BaseModel):
    filename: str
    game_context: GameContext


def _service(app) -> ExtractionService:
    return app.state.extraction_service


@router.post("/extract", response_model=ExtractResponse)
async def extract(payload: ExtractRequest, request: Request) -> ExtractResponse:
    try:
        game_context = await _service(request.app).extract(
            payload.source_url,
            payload.filename,
            payload.model,
            document_id=payload.document_id,
        )
    except UnsupportedDocumentError as error:
        raise HTTPException(status_code=415, detail=str(error)) from error
    except DocumentFetchError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except GameContextExtractionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except openai.APIError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return ExtractResponse(filename=payload.filename, game_context=game_context)
