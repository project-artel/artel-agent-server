"""One model call of the project agent, for orchestration's loop.

Stateless: orchestration stores the conversation and sends it whole, executes the
tool calls through the browser, and calls again. Nothing is kept here.
"""

import logging
import uuid

import anthropic
import openai
from botocore.exceptions import BotoCoreError, ClientError
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.agents import AgentContext
from app.agents.project_agent import ProjectAgent, ProjectAgentRequest, ProjectAgentResult
from app.llm.chat_model import (
    ClaudeApiKeyMissingError,
    GptApiKeyMissingError,
    key_service_of,
)
from app.llm.credentials import LlmKeyService, bind_llm_key
from app.llm.usage import set_usage_scope

logger = logging.getLogger(__name__)

LLM_KEY_MISSING = "llm_key_missing"
LLM_AUTH_FAILED = "llm_auth_failed"
LLM_RATE_LIMITED = "llm_rate_limited"
LLM_MODEL_UNAVAILABLE = "llm_model_unavailable"
LLM_PROVIDER_ERROR = "llm_provider_error"

# 응답에 실리는 문장. provider 의 오류 문구는 key 일부를 담을 수 있어 싣지 않는다.
_MESSAGES = {
    LLM_KEY_MISSING: "No API key is available for the selected model.",
    LLM_AUTH_FAILED: "The provider rejected the API key.",
    LLM_RATE_LIMITED: "The provider rate limit was reached.",
    LLM_MODEL_UNAVAILABLE: "The selected model is not available.",
    LLM_PROVIDER_ERROR: "The model provider call failed.",
}


def _detail(code: str) -> dict[str, str]:
    return {"code": code, "message": _MESSAGES[code]}


class _ProjectAgentRoute(APIRoute):
    """Answers an invalid `model` with a cause code. Other validation errors keep FastAPI's shape."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def wrapped(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as error:
                if any(tuple(e.get("loc", ()))[:2] == ("body", "model") for e in error.errors()):
                    return JSONResponse(
                        status_code=422, content={"detail": _detail(LLM_MODEL_UNAVAILABLE)}
                    )
                raise

        return wrapped


router = APIRouter(tags=["project-agent"], route_class=_ProjectAgentRoute)

_PROVIDER_ERRORS = (
    openai.APIError,
    anthropic.APIError,
    BotoCoreError,
    ClientError,
    ClaudeApiKeyMissingError,
    GptApiKeyMissingError,
)


def classify_provider_error(error: Exception) -> str:
    """The cause code of a provider failure."""
    if isinstance(error, (ClaudeApiKeyMissingError, GptApiKeyMissingError)):
        return LLM_KEY_MISSING
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        status = error.status_code
        if status in (401, 403):
            # OpenRouter answers a request that carried no key with this sentence.
            if "missing authentication header" in str(error).lower():
                return LLM_KEY_MISSING
            return LLM_AUTH_FAILED
        if status == 429:
            return LLM_RATE_LIMITED
        if status == 404:
            return LLM_MODEL_UNAVAILABLE
    return LLM_PROVIDER_ERROR


def _agent(app) -> ProjectAgent:
    return app.state.project_agent


@router.post("/project-agent/step", response_model=ProjectAgentResult)
async def project_agent_step(
    payload: ProjectAgentRequest, request: Request
) -> ProjectAgentResult:
    # `gpt/` has no platform key; fail now instead of at the first model call.
    if (
        payload.model is not None
        and key_service_of(payload.model) is LlmKeyService.openai
        and (payload.llm_key is None or payload.llm_key.service is not LlmKeyService.openai)
    ):
        raise HTTPException(status_code=422, detail=_detail(LLM_KEY_MISSING))
    set_usage_scope("PROJECT_AGENT", payload.conversation_id)
    context = AgentContext(
        session_id=f"project-agent-{payload.conversation_id}-{uuid.uuid4().hex[:8]}",
        metadata={"conversation_id": payload.conversation_id, "project_id": payload.project_id},
    )
    # Bound for this call only: the key and its chat-model cache never outlive it.
    bind_llm_key(payload.llm_key)
    try:
        return await _agent(request.app).run(payload, context)
    except _PROVIDER_ERRORS as error:
        code = classify_provider_error(error)
        # The error text can quote part of a key, so only its type is logged.
        logger.warning("[project-agent] provider error: %s (%s)", type(error).__name__, code)
        raise HTTPException(status_code=502, detail=_detail(code)) from error
    finally:
        bind_llm_key(None)
