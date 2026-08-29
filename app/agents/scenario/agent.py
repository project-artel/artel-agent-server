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
from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable

from langgraph.errors import GraphRecursionError

from app.agents.base import AgentContext
from app.agents.scenario.cases import MAX_SEARCHES_PER_RUN, TestCaseSearchState
from app.agents.scenario.errors import ScenarioGenerationError
from app.agents.scenario.progress import ProgressCallback
from app.agents.scenario.prompt import build_messages, build_system_prompt
from app.agents.scenario.ordering import UnreachableClimb, unreachable_climbs
from app.agents.scenario.schemas import (
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioPlan,
)
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

# How many flows one turn may rewrite for order (ARTEL-648). Each is another model
# turn, and the session on the far side calls a turn dead after five minutes of
# silence — a result with many faults would otherwise multiply one turn into many and
# be reported as stalled. Three covers what the measured runs actually produce; past
# that the answer is wrong in a way one more rewrite is not going to settle.
MAX_REWRITES_PER_TURN = 3

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
            # The turn reports its own model turns (ARTEL-487). Orchestration sees
            # the tool frames but not the time between them, and that time is most
            # of the wait — see app/agents/scenario/progress.py.
            "callbacks": [ProgressCallback(channel)],
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
        return await self._settle_order(structured, request, tools, config)

    async def _settle_order(
        self,
        result: ScenarioAgentResult,
        request: ScenarioAgentRequest,
        tools: list,
        config: dict,
    ) -> ScenarioAgentResult:
        """Ask again for the one flow that climbs without paying for it (ARTEL-648).

        The turn checks its own work. A flow that requires `StagePosition >= 1` and
        later `>= 2`, with nothing in between that raises it, never runs past the
        second check — and the model writes those even with the rule in front of it
        (run 208: eight of them). Grouping is a judgement about one case; ordering
        asks it to hold the whole list at once, and that is the part that slips.

        **Only the wrong flows are rewritten**, one at a time, with the reason named.
        Everything else is left exactly as authored — a turn that reshuffles work the
        model got right trades one problem for another.

        Failing here is not fatal. The unordered result is what we had a moment ago,
        and orchestration still sees it; a repair that errors should not lose the turn.
        """
        problems = unreachable_climbs(result.scenarios, request.test_case_list)
        if not problems:
            return result

        logger.info(
            "[scenario] climbs nothing pays for — asking again\n  scenarios=%s",
            {result.scenarios[i].title: [c.describe() for c in cs] for i, cs in problems.items()},
        )
        scenarios = list(result.scenarios)
        # **A bound on the worst case.** Each rewrite is another model turn, and the
        # session has a liveness deadline on the far side; a result with many faults
        # would otherwise multiply one turn into many and be reported as dead.
        for index, climbs in list(problems.items())[:MAX_REWRITES_PER_TURN]:
            fixed = await self._rewrite_one(scenarios[index], climbs, request, tools, config)
            if fixed is not None:
                scenarios[index] = fixed
        return result.model_copy(update={"scenarios": scenarios})

    async def _rewrite_one(
        self,
        scenario: ScenarioPlan,
        climbs: list[UnreachableClimb],
        request: ScenarioAgentRequest,
        tools: list,
        config: dict,
    ) -> ScenarioPlan | None:
        """One flow, rewritten with its own fault named. None when it could not be.

        **Its own agent, holding only this flow's cases.** The first pass needs the
        whole list — you cannot judge forty-two cases in or out against a population
        you cannot see. Ordering needs the opposite: measured, the same model given
        one journey's four-to-seven cases settled the order in 70-100 seconds, and the
        same question asked of the turn's own agent — still carrying all forty-two —
        ran eight minutes and came back empty, past the session's liveness deadline.

        The tools ride along unchanged. The structured answer itself arrives as a tool
        call (`ToolStrategy`), and a factory handed an empty list returned no structured
        response at all — measured, seven seconds and nothing (run 210). What this pass
        narrows is the prompt, not the loop.
        """
        mine = {step.case_id for step in scenario.steps if step.case_id is not None}
        narrowed = request.model_copy(
            update={"test_case_list": [c for c in request.test_case_list if c.id in mine]}
        )
        prompt, _ = build_system_prompt(narrowed)
        agent = self._agent_factory(model=request.model, tools=tools, system_prompt=prompt)
        told = "\n".join(f"- {climb.describe()}" for climb in climbs)
        ask = (
            f"방금 낸 시나리오 「{scenario.title}」 하나만 다시 씁니다. 실행되지 않는 자리가 "
            f"있습니다:\n\n{told}\n\n"
            "그 값을 올리는 화면을 지나는 스텝을 **그 사이에** 넣어 주세요. `case_id` 없는 "
            "연결 스텝이면 됩니다 — 예를 들어 그 화면에 들어가 조건을 만드는 동작입니다. "
            "케이스를 빼거나 다른 시나리오로 옮기지 말고, 스텝 순서와 사이만 고칩니다.\n\n"
            "`scenarios` 에는 이 시나리오 **하나만** 담고, 제목은 그대로 두세요. "
            "`reviewed` 는 비워 두세요 — 판정은 앞 턴 것을 씁니다."
        )
        try:
            state = await agent.ainvoke({"messages": [HumanMessage(content=ask)]}, config)
        except Exception as error:  # noqa: BLE001 — a failed repair must not lose the turn
            logger.warning("[scenario] rewrite failed, keeping the original: %s", error)
            return None
        answer = state.get("structured_response") if isinstance(state, dict) else None
        if not isinstance(answer, ScenarioAgentResult) or not answer.scenarios:
            # **무엇이 돌아왔는지 적는다.** 건수만 남기면 "안 됐다"와 "왜 안 됐다"가 같아 보이고,
            # 그러면 다음 판이 또 추측이 된다 — 오케가 거절 사유를 안 찍어 하루를 잃은 것과 같다
            # (ARTEL-641). 모델이 말은 했는데 시나리오를 안 담은 것인지, 구조화 답이 아예 안 온
            # 것인지가 갈린다.
            logger.warning(
                "[scenario] rewrite gave nothing to use, keeping the original\n"
                "  structured=%s scenarios=%d message=%s",
                type(answer).__name__,
                len(answer.scenarios) if isinstance(answer, ScenarioAgentResult) else -1,
                (answer.message[:300] if isinstance(answer, ScenarioAgentResult) else None),
            )
            return None
        # Keep the identity we already had. A rewrite that renames or re-points the
        # scenario would land as a different row on the other side.
        return answer.scenarios[0].model_copy(
            update={"scenario_id": scenario.scenario_id, "title": scenario.title}
        )
