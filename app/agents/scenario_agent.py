import json

from app.agents.base import AgentContext
from app.agents.scenario_prompt import ScenarioPromptBuilder
from app.agents.scenario_schemas import ScenarioAgentRequest, ScenarioAgentResult
from app.llm.json_schema import (
    build_strict_response_format,
    json_object_response_format,
)
from app.llm.models import DEFAULT_MODEL, LLMModel, get_model_spec
from app.llm.schemas import LLMRequest


class ScenarioAgent:
    def __init__(
        self,
        prompt_builder: ScenarioPromptBuilder | None = None,
        model: LLMModel = DEFAULT_MODEL,
    ) -> None:
        self._prompt_builder = prompt_builder or ScenarioPromptBuilder()
        self._model = model

    async def run(
        self,
        request: ScenarioAgentRequest,
        context: AgentContext,
    ) -> ScenarioAgentResult:
        llm_response = await context.llm.complete(
            LLMRequest(
                model=self._model.value,
                messages=self._prompt_builder.build(request),
                temperature=0.2,
                response_format=self._response_format(),
            )
        )
        payload = json.loads(llm_response.content)
        return ScenarioAgentResult.model_validate(payload)

    def _response_format(self) -> dict:
        if get_model_spec(self._model).supports_strict_json:
            return build_strict_response_format(
                ScenarioAgentResult,
                name="scenario_agent_result",
            )
        return json_object_response_format()
