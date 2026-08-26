from collections.abc import Callable

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.game_context.errors import GameContextExtractionError
from app.agents.game_context.prompt import (
    build_chain_inputs,
    build_game_context_prompt,
)
from app.agents.game_context.schemas import GameContext, GameContextAgentRequest
from app.llm.chat_model import build_chat_model, select_structured_method
from app.llm.models import LLMModel


_MAX_ATTEMPTS = 5

StructuredFactory = Callable[[LLMModel], Runnable]


def _default_structured_factory(model: LLMModel) -> Runnable:
    chat = build_chat_model(model)
    if select_structured_method(model) == "json_schema":
        return chat.with_structured_output(
            GameContext, method="json_schema", strict=True
        )
    return chat.with_structured_output(GameContext, method="json_mode")


class GameContextAgent:
    """Extracts a structured ``GameContext`` from one design document's text.

    Single LLM pass per document; cross-document merging is the aggregation
    layer's job, not the agent's. ``structured_factory`` is injectable so tests
    can supply a canned runnable instead of calling a real model.
    """

    def __init__(self, structured_factory: StructuredFactory | None = None) -> None:
        self._prompt = build_game_context_prompt()
        self._structured_factory = structured_factory or _default_structured_factory

    async def run(
        self,
        request: GameContextAgentRequest,
        context: AgentContext,
    ) -> GameContext:
        structured = self._structured_factory(request.model)
        chain = (self._prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            return await chain.ainvoke(
                build_chain_inputs(request),
                context.trace_config("game-context-extraction"),
            )
        except OutputParserException as error:
            raise GameContextExtractionError(
                "Failed to produce valid game_context JSON."
            ) from error
