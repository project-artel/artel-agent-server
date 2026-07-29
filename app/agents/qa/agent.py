from collections.abc import Callable

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from app.agents.base import AgentContext
from app.agents.qa.errors import QaExecutionError
from app.agents.qa.prompt import (
    build_act_inputs,
    build_act_prompt,
    build_chat_inputs,
    build_chat_prompt,
    build_evaluate_inputs,
    build_evaluate_prompt,
)
from app.agents.qa.schemas import (
    QaActRequest,
    QaActResult,
    QaChatRequest,
    QaChatResult,
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
        self._chat_prompt = build_chat_prompt()
        self._structured_factory = structured_factory or _default_structured_factory

    async def act(self, request: QaActRequest, context: AgentContext) -> QaActResult:
        """Plan the concrete actions that carry out the current step."""
        structured = self._structured_factory(request.model, QaActResult)
        chain = (self._act_prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            return await chain.ainvoke(
                build_act_inputs(request), context.trace_config("qa-action")
            )
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
            return await chain.ainvoke(
                build_evaluate_inputs(request), context.trace_config("qa-evaluation")
            )
        except OutputParserException as error:
            raise QaExecutionError("Failed to produce a valid QA verdict.") from error

    async def respond(self, request: QaChatRequest, context: AgentContext) -> QaChatResult:
        """Answer the operator without touching the game.

        Talking is separated from acting on purpose: this turn must never emit an
        ACTION. What the operator says is carried into the next act/evaluate
        through the request's `chat`, which is where it changes behaviour.
        """
        structured = self._structured_factory(request.model, QaChatResult)
        chain = (self._chat_prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            return await chain.ainvoke(
                build_chat_inputs(request), context.trace_config("qa-operator-response")
            )
        except OutputParserException as error:
            raise QaExecutionError("Failed to produce a QA chat reply.") from error
