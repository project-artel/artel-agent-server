"""One model call of the project agent, for orchestration's loop.

Stateless: orchestration stores the conversation and sends it whole, executes the
tool calls through the browser, and calls again. Nothing is kept here.
"""

import logging
import uuid

import anthropic
import openai
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request

from app.agents import AgentContext
from app.agents.project_agent import ProjectAgent, ProjectAgentRequest, ProjectAgentResult
from app.llm.chat_model import ClaudeApiKeyMissingError, GptApiKeyMissingError
from app.llm.usage import set_usage_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["project-agent"])

_PROVIDER_ERRORS = (
    openai.APIError,
    anthropic.APIError,
    BotoCoreError,
    ClientError,
    ClaudeApiKeyMissingError,
    GptApiKeyMissingError,
)


def _agent(app) -> ProjectAgent:
    return app.state.project_agent


@router.post("/project-agent/step", response_model=ProjectAgentResult)
async def project_agent_step(
    payload: ProjectAgentRequest, request: Request
) -> ProjectAgentResult:
    set_usage_scope("PROJECT_AGENT", payload.conversation_id)
    context = AgentContext(
        session_id=f"project-agent-{payload.conversation_id}-{uuid.uuid4().hex[:8]}",
        metadata={"conversation_id": payload.conversation_id, "project_id": payload.project_id},
    )
    try:
        return await _agent(request.app).run(payload, context)
    except _PROVIDER_ERRORS as error:
        logger.warning("[project-agent] provider error: %s", error)
        raise HTTPException(status_code=502, detail=str(error)) from error
