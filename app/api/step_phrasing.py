"""Rephrasing one user sentence into the steps that fill an authoring gap.

Not part of a session. The authoring session is a conversation with memory and
tools; this is a single sentence with no history, called from the code path that
already knows where the answer goes. Running it through a turn would put the
placement back in the model's hands — which is the bug this exists to keep fixed.
"""

import uuid

import openai
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.agents import (
    AgentContext,
    PhrasedStep,
    StepPhrasingAgent,
    StepPhrasingError,
    StepPhrasingRequest,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.usage import set_usage_scope

router = APIRouter(tags=["scenario"])


class StepPhrasingBody(BaseModel):
    said: str
    blocked_by: str = ""
    before: str = ""
    after: str = ""
    locale: str = "ko"
    model: LLMModel = DEFAULT_MODEL
    # What this call's LLM spend is booked against, like /knowledge-queries.
    project_id: int | None = None


class StepPhrasingResponse(BaseModel):
    """Zero or more steps. Empty means the sentence did not describe a way across."""

    steps: list[PhrasedStep]


def _agent(app) -> StepPhrasingAgent:
    return app.state.step_phrasing_agent


@router.post("/scenario-steps/phrase", response_model=StepPhrasingResponse)
async def phrase_steps(
    payload: StepPhrasingBody, request: Request
) -> StepPhrasingResponse:
    set_usage_scope("SCENARIO", None)
    # session_id is correlation-only; phrasing is stateless.
    context = AgentContext(session_id=f"step-phrasing-{uuid.uuid4().hex}")
    try:
        steps = await _agent(request.app).run(
            StepPhrasingRequest(
                said=payload.said,
                blocked_by=payload.blocked_by,
                before=payload.before,
                after=payload.after,
                locale=payload.locale,
                model=payload.model,
            ),
            context,
        )
    except StepPhrasingError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except openai.APIError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return StepPhrasingResponse(steps=steps)
