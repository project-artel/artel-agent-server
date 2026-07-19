import json

from app.agents.base import AgentContext
from app.agents.scenario_prompt import ScenarioPromptBuilder
from app.agents.scenario_schemas import ScenarioAgentRequest, ScenarioAgentResult
from app.llm.schemas import LLMRequest


class ScenarioAgent:
    def __init__(
        self,
        prompt_builder: ScenarioPromptBuilder | None = None,
        model: str = "openrouter/auto",
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
                model=self._model,
                messages=self._prompt_builder.build(request),
                temperature=0.2,
            )
        )
        payload = json.loads(llm_response.content)
        return ScenarioAgentResult.model_validate(payload)
