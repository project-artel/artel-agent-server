"""The scenario authoring turn as a tool loop.

Where v1 was a single structured LLM call, this drives a `create_agent` tool
loop, and the structured final answer is produced by a `ToolStrategy` response
format rather than by a `finish_run`-style tool (mirroring
`app/agents/qa/runner.py`).

How many turns the loop actually takes depends on what the session was given.
With the project's test case list in the prompt (ARTEL-319) there are no tools at
all and the loop is one model turn straight to the structured answer; without it
the agent searches existing TestCases with `search_test_cases` over the session
channel as many times as its budget allows first. The step budget below is sized
for the second, longer shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.runnables import Runnable

from langgraph.errors import GraphRecursionError

from app.agents.base import AgentContext
from app.agents.scenario.cases import MAX_SEARCHES_PER_RUN, TestCaseSearchState
from app.agents.scenario.errors import ScenarioGenerationError
from app.agents.scenario.prompt import build_messages, build_system_prompt
from app.agents.scenario.schemas import ScenarioAgentRequest, ScenarioAgentResult
from app.agents.scenario.tools import build_tools
from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel

if TYPE_CHECKING:
    # Type-only: see app/agents/scenario/cases.py for why app.sessions is not
    # imported at module load.
    from app.sessions.channel import ScenarioChannel

logger = logging.getLogger(__name__)

# Graph steps the loop may take: a model turn and a tool turn per search, the
# structured-output turn, and headroom. recursion_limit counts graph steps, so
# this bounds tool calls and model turns together (see the QA runner). Doubled to
# cover the model turn that sits between each tool result. The headroom is generous
# so a model that keeps probing after the search budget still lands on the
# structured output before the limit — and if it doesn't, we fail gracefully below.
RECURSION_LIMIT = (MAX_SEARCHES_PER_RUN + 8) * 2

# Injectable so tests can hand back a canned runnable instead of reaching a real
# model. The runnable is invoked with {"messages": [...]} and must return a state
# dict carrying "structured_response".
AgentFactory = Callable[..., Runnable]


def _default_agent_factory(
    *, model: LLMModel, tools: list, system_prompt: str
) -> Runnable:
    # ToolStrategy rather than the provider's native structured output: the loop
    # already uses tool calling for the search, and a structured-output tool
    # alongside it is the portable path across the OpenRouter catalog — the model
    # calls it to return the final plan, which ends the loop.
    return create_agent(
        model=build_chat_model(model),
        tools=tools,
        system_prompt=system_prompt,
        response_format=ToolStrategy(schema=ScenarioAgentResult),
    )


class ScenarioAgent:
    """Turn-level multi-scenario authoring agent backed by a tool loop.

    ``agent_factory`` is injectable so tests can supply a canned runnable instead
    of calling a real model.
    """

    def __init__(self, agent_factory: AgentFactory | None = None) -> None:
        self._agent_factory = agent_factory or _default_agent_factory

    async def run(
        self,
        request: ScenarioAgentRequest,
        context: AgentContext,
        channel: ScenarioChannel,
    ) -> ScenarioAgentResult:
        state = TestCaseSearchState()
        # A session with the list gets no tools — the cases are in the prompt, so the
        # loop is a single model turn straight to the structured answer.
        tools = build_tools(channel, state, has_test_case_list=bool(request.test_case_list))
        system_prompt, version = build_system_prompt(request)
        messages = build_messages(request)

        logger.info(
            "[scenario] turn starting\n"
            "  model=%s locale=%s prompt_version=%s test_case_list=%d tools=%s",
            request.model,
            request.locale,
            version,
            len(request.test_case_list),
            [tool.name for tool in tools],
        )

        agent = self._agent_factory(
            model=request.model, tools=tools, system_prompt=system_prompt
        )
        config = {
            **context.trace_config("scenario-generation"),
            "recursion_limit": RECURSION_LIMIT,
        }
        try:
            result_state = await agent.ainvoke({"messages": messages}, config)
        except GraphRecursionError as error:
            # The tool loop never converged on a structured result within the step
            # budget (e.g. the model kept searching). Surface it as a generation
            # failure so the session emits an error frame and the client unblocks,
            # rather than the turn task dying silently and leaving the UI "thinking".
            logger.warning("[scenario] recursion limit hit before a result: %s", error)
            raise ScenarioGenerationError(
                "Scenario agent kept searching without settling on scenarios. "
                "Try a more specific request."
            ) from error

        structured = (
            result_state.get("structured_response")
            if isinstance(result_state, dict)
            else None
        )
        if not isinstance(structured, ScenarioAgentResult):
            # The loop ended without a structured plan — the model stopped early,
            # or the structured-output tool never came back. Nothing downstream
            # can act on that, so it is a generation failure, not an empty result.
            raise ScenarioGenerationError(
                "Scenario agent did not return a structured multi-scenario result."
            )
        return structured
