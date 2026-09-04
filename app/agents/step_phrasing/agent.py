from collections.abc import Callable

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.step_phrasing.errors import StepPhrasingError
from app.agents.step_phrasing.prompt import build_chain_inputs, build_step_phrasing_prompt
from app.agents.step_phrasing.schemas import PhrasedStep, PhrasedSteps, StepPhrasingRequest
from app.llm.chat_model import structured
from app.llm.models import LLMModel

_MAX_ATTEMPTS = 2

# More than this from one sentence is not a rephrasing any more. The user typed a
# line; a model that answers with a scenario has started writing the test itself.
MAX_STEPS = 6

StructuredFactory = Callable[[LLMModel], Runnable]


def _default_structured_factory(model: LLMModel) -> Runnable:
    return structured(model, PhrasedSteps)


class StepPhrasingAgent:
    """Turns what a user said about a gap into the steps that go in it.

    One model call, no tools, no memory: the whole input is one sentence and its
    two neighbours. It is deliberately the smallest agent in the server, because
    everything it could usefully "decide" is already decided — where the steps
    go, whether they are graded, what they replace.

    ``structured_factory`` is injectable so tests can supply a canned runnable
    instead of calling a real model.
    """

    def __init__(self, structured_factory: StructuredFactory | None = None) -> None:
        self._prompt = build_step_phrasing_prompt()
        self._structured_factory = structured_factory or _default_structured_factory

    async def run(
        self, request: StepPhrasingRequest, context: AgentContext
    ) -> list[PhrasedStep]:
        if not request.said.strip():
            return []
        structured = self._structured_factory(request.model)
        chain = (self._prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            phrased = await chain.ainvoke(
                build_chain_inputs(request),
                context.trace_config("step-phrasing"),
            )
        except OutputParserException as error:
            # The caller falls back to the user's own sentence, so this is not a
            # dead end for them — but it must be visible as a failure, not as
            # "the user said nothing usable".
            raise StepPhrasingError("The model did not return usable steps.") from error

        steps = [
            PhrasedStep(action=step.action.strip(), input=(step.input or "").strip() or None)
            for step in phrased.steps
            if step.action.strip()
        ]
        return steps[:MAX_STEPS]
