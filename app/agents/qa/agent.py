from collections.abc import Callable

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from app.agents.base import AgentContext
from app.agents.qa.errors import QaExecutionError
from app.agents.qa.prompt import (
    build_act_inputs,
    build_act_prompt,
    build_evaluate_inputs,
    build_evaluate_prompt,
)
from app.agents.qa.schemas import (
    QaActRequest,
    QaActResult,
    QaVerifyRequest,
    QaVerifyResult,
)
from app.llm.chat_model import build_chat_model, select_structured_method
from app.llm.models import LLMModel


_MAX_ATTEMPTS = 2

StructuredFactory = Callable[[LLMModel, type[BaseModel]], Runnable]


def _default_structured_factory(model: LLMModel, schema: type[BaseModel]) -> Runnable:
    chat = build_chat_model(model)
    if select_structured_method(model) == "json_schema":
        return chat.with_structured_output(schema, method="json_schema", strict=True)
    return chat.with_structured_output(schema, method="json_mode")


class QaExecutionAgent:
    """Drives one approved test scenario, step by step, over a live game.

    Two decision points, both plain async structured calls (transport-agnostic —
    the WebSocket lives in the API layer). ``structured_factory`` is injectable so
    tests can supply a canned runnable instead of calling a real model.
    """

    def __init__(self, structured_factory: StructuredFactory | None = None) -> None:
        self._act_prompt = build_act_prompt()
        self._evaluate_prompt = build_evaluate_prompt()
        self._structured_factory = structured_factory or _default_structured_factory

    async def act(self, request: QaActRequest, context: AgentContext) -> QaActResult:
        """Plan the concrete actions that carry out the current step."""
        structured = self._structured_factory(request.model, QaActResult)
        chain = (self._act_prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            return await chain.ainvoke(build_act_inputs(request))
        except OutputParserException as error:
            raise QaExecutionError("Failed to produce a valid QA action.") from error

    async def evaluate(
        self, request: QaVerifyRequest, context: AgentContext
    ) -> QaVerifyResult:
        """Judge whether the step's `expected` result held."""
        structured = self._structured_factory(request.model, QaVerifyResult)
        chain = (self._evaluate_prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            return await chain.ainvoke(build_evaluate_inputs(request))
        except OutputParserException as error:
            raise QaExecutionError("Failed to produce a valid QA verdict.") from error
