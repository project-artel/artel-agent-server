import asyncio

import openai
from fastapi import APIRouter, HTTPException, Request
from langchain_core.exceptions import OutputParserException

from app.demonstrations.schemas import ExtractionRequest, ExtractionResponse
from app.documents.fetch import DocumentFetchError
from app.llm.usage import set_usage_scope

router = APIRouter(tags=["demonstrations"])


@router.post("/demonstrations/extract", response_model=ExtractionResponse)
async def extract_demonstration(payload: ExtractionRequest, request: Request) -> ExtractionResponse:
    set_usage_scope("SCENARIO", None)
    try:
        return await asyncio.wait_for(
            request.app.state.demonstration_extraction_agent.run(payload), timeout=120,
        )
    except (ValueError, OutputParserException) as error:
        raise HTTPException(502, "Model returned an invalid verification procedure") from error
    except openai.APIError as error:
        raise HTTPException(502, "Demonstration extraction provider failed") from error
    except DocumentFetchError as error:
        raise HTTPException(502, "Demonstration evidence could not be read") from error
    except TimeoutError as error:
        raise HTTPException(504, "Demonstration extraction timed out") from error
