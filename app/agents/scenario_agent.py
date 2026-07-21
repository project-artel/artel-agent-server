import json

from pydantic import ValidationError

from app.agents.base import AgentContext
from app.agents.errors import ScenarioGenerationError
from app.agents.json_parse import extract_json_object
from app.agents.scenario_prompt import ScenarioPromptBuilder
from app.agents.scenario_schemas import ScenarioAgentRequest, ScenarioAgentResult
from app.llm.json_schema import (
    build_strict_response_format,
    json_object_response_format,
)
from app.llm.models import LLMModel, get_model_spec
from app.llm.schemas import LLMMessage, LLMRequest, MessageRole


_MAX_ATTEMPTS = 2
_CORRECTION_PROMPT = (
    "Your previous response was not valid JSON matching the output contract. "
    "Return ONLY corrected JSON that matches the contract, with no extra text."
)


class ScenarioAgent:
    def __init__(self, prompt_builder: ScenarioPromptBuilder | None = None) -> None:
        self._prompt_builder = prompt_builder or ScenarioPromptBuilder()

    async def run(
        self,
        request: ScenarioAgentRequest,
        context: AgentContext,
    ) -> ScenarioAgentResult:
        messages = self._prompt_builder.build(request)
        response_format = self._response_format(request.model)

        last_error: Exception | None = None
        for _ in range(_MAX_ATTEMPTS):
            response = await context.llm.complete(
                LLMRequest(
                    model=request.model.value,
                    messages=messages,
                    temperature=0.2,
                    response_format=response_format,
                )
            )
            try:
                payload = json.loads(extract_json_object(response.content))
                return ScenarioAgentResult.model_validate(payload)
            except (json.JSONDecodeError, ValidationError, ValueError) as error:
                last_error = error
                messages = [
                    *messages,
                    LLMMessage(
                        role=MessageRole.assistant, content=response.content
                    ),
                    LLMMessage(role=MessageRole.user, content=_CORRECTION_PROMPT),
                ]

        raise ScenarioGenerationError(
            "Failed to produce valid scenario JSON."
        ) from last_error

    def _response_format(self, model: LLMModel) -> dict:
        if get_model_spec(model).supports_strict_json:
            return build_strict_response_format(
                ScenarioAgentResult,
                name="scenario_agent_result",
            )
        return json_object_response_format()
