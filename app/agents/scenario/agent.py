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
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
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
from app.agents.scenario import trace
from app.agents.scenario.router import Route, ScenarioRouter
from app.agents.scenario.workflow import run_authoring_workflow, run_modify_workflow
from app.agents.scenario.schemas import (
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioPlan,
)
from app.agents.scenario.tools import build_tools
from app.config import get_settings
from app.llm.chat_model import build_chat_model, structured
from app.llm.models import LLMModel, ReasoningConfig

if TYPE_CHECKING:
    # Type-only: see app/agents/scenario/cases.py for why app.sessions is not
    # imported at module load.
    from app.sessions.channel import ScenarioChannel

logger = logging.getLogger(__name__)

# 한 턴이 걸을 수 있는 graph step 수: 조회마다 모델 turn 하나와 도구 turn 하나, 구조화 출력
# turn, 그리고 여유. `recursion_limit` 은 graph step 을 세므로 도구 호출과 모델 turn 을 함께
# 묶는다(QA runner 와 같다). 도구 결과 사이에 끼는 모델 turn 까지 덮으려고 두 배로 둔다.
#
# **일의 양에서 잡는다. 아무도 근거를 못 대는 수에서 잡지 않는다.** 앞서는
# `(MAX_SEARCHES_PER_RUN + 8) * 2` 였고 그 천장이 백만이라, 주석은 "어떤 turn 도 닿지 않는
# 천장"이라 적혀 있었지만 실제로는 아무것도 막지 못했다. 실측에서 한 turn 이 케이스 88건에
# `find_path` 를 280번 왕복했고 그중 109번이 이미 답을 받은 두 케이스를 다시 물은 것이었으며,
# 열네 분을 돌고도 끝나지 않았다.
#
# 일의 양을 정하는 것은 케이스 수다. 케이스 하나에 조회 두어 번이면 충분하고 — 사이는 흐름이
# 이미 싣고 온다 — 케이스마다 여러 번 물은 turn 은 수렴하지 않는 것이다. 그대로 두면 오지
# 않을 답을 기다리는 데 사용자의 몇 분을 쓴다.
LOOKUPS_PER_CASE = 3

# 목록 없는 turn 의 바닥값과 구조화 출력에 남길 여유.
BASE_LOOKUPS = 16


def recursion_limit_for(case_count: int) -> int:
    """한 turn 이 걸을 수 있는 graph step 수. 놓을 케이스가 몇 건인지에서 잡는다."""
    return (case_count * LOOKUPS_PER_CASE + BASE_LOOKUPS) * 2

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
    *, model: LLMModel, tools: list, system_prompt: str,
    reasoning: ReasoningConfig | None = None,
) -> Runnable:
    # ToolStrategy rather than the provider's native structured output: the loop
    # already uses tool calling for the search, and a structured-output tool
    # alongside it is the portable path across the OpenRouter catalog — the model
    # calls it to return the final plan, which ends the loop.
    # 저작은 한 턴이 여러 번의 도구 왕복으로 자란다. 케이스 목록과 system prompt 는 그 사이
    # 변하지 않으므로, 뒤 호출이 앞 호출의 prompt 를 되읽게 한다.
    return create_agent(
        # **저작도 추론을 켤 수 있다.** QA 런은 켜고 저작은 안 켜고 있었다 — 순서를 세우고
        # 무엇을 담을지 고르는 것은 한 번에 답이 나오는 일이 아니라, 싼 모델일수록 이 예산이
        # 답을 가른다. 안 주면 예전 그대로 없이 간다.
        model=build_chat_model(model, reasoning, cache_prompt=True),
        tools=tools,
        system_prompt=system_prompt,
        response_format=ToolStrategy(schema=ScenarioAgentResult),
        # **폭주 방지선은 코드가 든다**(워크플로 재편 ⑥). 프롬프트 지시는 보조일 뿐이다 —
        # 반려 9~10연속(런 31·40) 같은 헛바퀴가 왕복마다 전체 재읽기를 물었다. 모델 한도는
        # `end` 로 곱게 끝나 회수 장치(_structure_afterwards)가 기록을 건진다.
        middleware=limit_middleware(),
    )


def limit_middleware() -> list:
    """한 턴의 절대 천장 — 케이스 수와 무관한 폭주 방지선. 값은 Settings 에."""
    settings = get_settings()
    return [
        ModelCallLimitMiddleware(run_limit=settings.scenario_max_model_calls, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=settings.scenario_max_tool_calls),
    ]


# 라우터가 문구를 못 만들었을 때의 기본 응답 — v8 의 인사·가드레일 규칙과 같은 내용이다.
_CANNED = {
    Route.GREETING: (
        "안녕하세요! 저는 Artel 의 QA 저작 도우미예요. 설명해 주신 게임 흐름을 순서 있는 "
        "테스트 시나리오로 만들고, 이 런의 케이스에 매핑해 드려요. 어떤 흐름을 테스트하고 "
        "싶은지 말씀해 주세요."
    ),
    Route.OFFTOPIC: "그 주제는 제가 도와드리기 어려워요. 이 런의 시나리오 저작이나 케이스에 대해 물어봐 주세요!",
}


def _hint(request: ScenarioAgentRequest) -> str:
    """라우터에게 주는 문맥 한 줄 — 최근 대화의 마지막 사용자·어시스턴트 발화만."""
    tail = [m for m in request.history[-2:]]
    return " / ".join(str(getattr(m, "content", ""))[:120] for m in tail)


class ScenarioAgent:
    """Turn-level multi-scenario authoring agent backed by a tool loop.

    ``agent_factory`` is injectable so tests can supply a canned runnable instead
    of calling a real model.
    """

    def __init__(
        self,
        agent_factory: AgentFactory | None = None,
        router: ScenarioRouter | None = None,
    ) -> None:
        self._agent_factory = agent_factory or _default_agent_factory
        # 라우터 없음 = 예전 그대로(모든 입력이 루프로). 검사와 롤백이 이 기본값 하나로
        # 선다 — 켜는 쪽(세션 서비스)이 명시적으로 넣는다.
        self._router = router

    async def run(
        self,
        request: ScenarioAgentRequest,
        context: AgentContext,
        channel: ScenarioChannel,
    ) -> ScenarioAgentResult:
        # **입구 라우팅**(워크플로 재편 Step 1). 케이스 전량을 싣기 전에 갈래를 가른다 —
        # 현행의 낭비는 "안녕하세요"에도 74k 가 나가던 것이다. 모르면 AUTHORING 이고,
        # 그 갈래는 아래 루프 그대로라 오분류가 무동작이 되지 않는다.
        if self._router is not None and request.current_route is None:
            verdict = await self._router.route(request.user_input, context_hint=_hint(request))
            if verdict.route in (Route.GREETING, Route.OFFTOPIC):
                # 모델 없이 끝난다 — 라우터가 문구까지 만들었다.
                return ScenarioAgentResult(message=verdict.reply or _CANNED[verdict.route], scenarios=[])
            if verdict.route is Route.AUTHORING and get_settings().scenario_workflow_enabled:
                # **본선 워크플로**(Step 2): B 묶기·순서(전량 1회) → C 병렬 문장(묶음만)
                # → D 코드 제출. "제출 N번 = 전체 재읽기 N번"이 여기서 사라진다.
                return await run_authoring_workflow(request, context, channel)
            if verdict.route is Route.MODIFY and get_settings().scenario_workflow_enabled:
                # **E 수정 갈래**(Step 3): 오케가 실어 준 current_scenarios 에서 대상을
                # 가리키고, scenario_id 교체 저장으로 되돌린다. 모델 1회 + 반려 시 1회.
                return await run_modify_workflow(request, context, channel)
            if verdict.route is Route.QUESTION:
                # **새 기계가 아니라 설계된 폴백을 탄다**: 전량을 비우면 프롬프트가
                # 검색 안내(NO_TEST_CASE_LIST_NOTICE)로 갈리고 검색 도구가 켜진다
                # (ARTEL-319 의 롤백 경로). 케이스 74k·흐름·간선이 전부 빠진 소형 턴이다.
                slim = request.model_copy(update={
                    "test_case_list": [],
                    "flows": [],
                    "scene_edges": [],
                    "current_route": Route.QUESTION.value,
                })
                return await self.run(slim, context, channel)
            if not get_settings().scenario_loop_enabled:
                # **구 루프 갈래 끊기**(실험 전용 — 서비스 기본값은 켬). 여기 닿는 것은
                # MODIFY 와, 워크플로가 꺼진 채의 저작뿐이다. 지금은 그 요청이 얼마나,
                # 어떤 모양으로 오는지를 **기록만 하고** 전량 프롬프트를 태우지 않는다 —
                # 이 기록이 Step 3(E 수정 갈래)의 재료다.
                trace.record(
                    request.run_id, "구 루프 갈래 — 끊음(기록만)",
                    f"route={verdict.route.value} · {request.user_input[:200]}",
                )
                if request.locale.value == "ko":
                    message = (
                        "이 요청은 기존 시나리오 수정 갈래로 분류되었어요. 지금은 실험 "
                        "중이라 이 갈래를 잠시 꺼 두어 저작을 진행하지 않았습니다 — "
                        "요청 내용은 기록해 두었어요."
                    )
                else:
                    message = (
                        "This request was routed to the legacy modify branch, which is "
                        "switched off during the experiment. Nothing was authored — the "
                        "request itself has been recorded."
                    )
                return ScenarioAgentResult(message=message, scenarios=[])

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

        # The prompt is the one thing orchestration cannot see — it composes the
        # pieces but never the assembled text, and "what did the model actually
        # read" is the first question every wrong answer raises (ARTEL-650).
        trace.record(
            request.run_id,
            "  · 모델에 보낸다",
            f"프롬프트 {version} {trace.blob(request.run_id, 'prompt.md', system_prompt)}\n"
            f"도구: {[tool.name for tool in tools]}",
        )

        agent = self._agent_factory(
            model=request.model, tools=tools, system_prompt=system_prompt,
            reasoning=request.reasoning,
        )
        config = {
            **context.trace_config("scenario-generation"),
            "recursion_limit": recursion_limit_for(len(request.test_case_list)),
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
            #
            # Log what the model actually said before discarding it. From the
            # outside this failure reads the same whatever the model did —
            # refused, answered in prose, ran dry — and the difference is the
            # whole diagnosis. Measured: Bedrock + thinking downgrades a forced
            # tool_choice to auto, and whether the model then volunteers the
            # structured call is exactly what this line shows.
            tail = result_state.get("messages", [])[-1:] if isinstance(result_state, dict) else []
            for message in tail:
                logger.warning(
                    "[scenario] no structured result; last message (%s) "
                    "stop=%s usage=%s tool_calls=%s: %.2000s",
                    type(message).__name__,
                    getattr(message, "response_metadata", {}).get("stopReason"),
                    getattr(message, "usage_metadata", None),
                    getattr(message, "tool_calls", None),
                    getattr(message, "content", ""),
                )
            structured = await self._structure_afterwards(request, result_state)
        if not isinstance(structured, ScenarioAgentResult):
            raise ScenarioGenerationError(
                "Scenario agent did not return a structured multi-scenario result."
            )
        return await self._settle_order(structured, request, tools, config)

    async def _structure_afterwards(
        self, request: ScenarioAgentRequest, result_state: Any
    ) -> ScenarioAgentResult | None:
        """루프가 구조화 답 없이 끝났을 때, 기록을 들고 한 번 더 계약대로 시킨다.

        Bedrock 의 Anthropic 모델은 thinking 이 켜지면 `tool_choice` 강제를 못 받는다
        (API 제약 — langchain 이 `auto` 로 낮춘다). 그러면 마지막 답을 도구로 내는
        `ToolStrategy` 계약이 강제가 아니라 부탁이 되고, 실측(런 16, Haiku)에서 모델이
        10,545 token 을 생각한 뒤 도구도 본문도 없이 턴을 끝냈다.

        그 생각을 버리지 않는다 — QA 런이 같은 조건에서 판정을 받아내는 길
        (`structured()`, ARTEL-806)로 기록을 넘겨 결론만 계약 모양으로 다시 받는다.
        생각은 이미 돼 있으므로 이 호출은 옮겨 적기다. 한 번만 시도하고, 그래도
        안 되면 원래대로 실패다.
        """
        transcript = self._transcript_text(result_state)
        if not transcript:
            return None
        try:
            answer = await structured(request.model, ScenarioAgentResult).ainvoke(
                "아래는 게임 QA 시나리오 저작 에이전트가 한 턴 동안 남긴 작업 기록이다. "
                "기록이 도달한 결론을 계약 스키마 그대로 정리하라. 기록에 없는 것을 지어내지 "
                "말고, 시나리오가 만들어지지 않았다면 scenarios 를 비우고 message 에 이유를 "
                f"적어라. 사용자의 요청: {request.user_input}\n\n{transcript}"
            )
        except Exception as error:  # noqa: BLE001 — 실패하면 원래의 실패로 돌아간다
            logger.warning("[scenario] structuring the transcript failed: %s", error)
            return None
        if isinstance(answer, ScenarioAgentResult):
            logger.info("[scenario] structured the transcript after the loop declined to")
            return answer
        return None

    @staticmethod
    def _transcript_text(result_state: Any) -> str:
        """루프가 남긴 메시지를 글 한 덩이로 편다.

        thinking block 을 그대로 되보내면 Anthropic 이 마지막 assistant 턴의 서명을
        검사하므로, 글로 펴서 새 대화로 넘긴다 — 내용만 필요하고 형식은 필요 없다.
        """
        if not isinstance(result_state, dict):
            return ""
        lines: list[str] = []
        for message in result_state.get("messages", []):
            content = getattr(message, "content", "")
            if isinstance(content, list):
                parts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "reasoning_content":
                        parts.append(block.get("reasoning_content", {}).get("text", ""))
                    elif block.get("type") == "text":
                        parts.append(block.get("text", ""))
                content = "\n".join(part for part in parts if part)
            if content:
                lines.append(f"[{getattr(message, 'type', '?')}] {content}")
        return "\n\n".join(lines)

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
            trace.record(request.run_id, "  · 스스로 되짚었다", "못 가는 오름 없음")
            return result
        trace.record(
            request.run_id,
            "  · 스스로 되짚었다",
            "\n".join(
                f"{result.scenarios[i].title}: " + " / ".join(c.describe() for c in cs)
                for i, cs in problems.items()
            ),
        )

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
