from collections.abc import Callable

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.scenario.errors import ScenarioGenerationError
from app.agents.scenario.prompt import build_chain_inputs, build_scenario_prompt
from app.agents.scenario.schemas import ScenarioAgentRequest, ScenarioAgentResult
from app.llm.chat_model import build_chat_model, select_structured_method
from app.llm.models import LLMModel


_MAX_ATTEMPTS = 2

StructuredFactory = Callable[[LLMModel], Runnable]


def _default_structured_factory(model: LLMModel) -> Runnable:
    chat = build_chat_model(model)
    if select_structured_method(model) == "json_schema":
        return chat.with_structured_output(
            ScenarioAgentResult, method="json_schema", strict=True
        )
    return chat.with_structured_output(ScenarioAgentResult, method="json_mode")


class ScenarioAgent:
    """Turn-level scenario generator backed by a LangChain structured chain.

    ``structured_factory`` is injectable so tests can supply a canned runnable
    instead of calling a real model.
    """

    def __init__(self, structured_factory: StructuredFactory | None = None) -> None:
        self._prompt = build_scenario_prompt()
        self._structured_factory = structured_factory or _default_structured_factory

    async def run(
        self,
        request: ScenarioAgentRequest,
        context: AgentContext,
    ) -> ScenarioAgentResult:
        structured = self._structured_factory(request.model)
        chain = (self._prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            return await chain.ainvoke(
                build_chain_inputs(request),
                context.trace_config("scenario-generation"),
            )
        except OutputParserException as error:
            raise ScenarioGenerationError(
                "Failed to produce valid scenario JSON."
            ) from error
