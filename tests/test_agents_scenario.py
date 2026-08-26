import asyncio

import pytest
from langchain_core.runnables import RunnableLambda
from langchain_core.tracers.context import collect_runs

from app.agents import (
    AgentContext,
    OutputLanguage,
    ScenarioAgent,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioGenerationError,
    ScenarioPlan,
)
from app.agents.scenario.cases import NO_TEST_CASE_LIST_NOTICE, render_test_case_list
from app.agents.scenario.schemas import (
    AuthoredStep,
    ReviewedCases,
    ScenarioAgentResult,
    TestCaseListItem,
)
from app.agents.scenario.prompt import (
    LANGUAGE_DIRECTIVES,
    build_first_message,
    build_system_prompt,
)
from app.llm.chat_model import select_structured_method
from app.prompts import load_prompt
from app.llm.models import LLMModel
from app.sessions.channel import ScenarioChannel


def _result(message: str = "Authored two scenarios.") -> ScenarioAgentResult:
    return ScenarioAgentResult(
        message=message,
        scenarios=[
            ScenarioPlan(
                title="Login reward flow",
                description="Verify the login reward flow.",
                steps=[
                    AuthoredStep(action="Open the login reward popup", case_id=11),
                    AuthoredStep(action="Observe the reward is granted", case_id=11),
                    AuthoredStep(action="Return to the lobby"),
                    AuthoredStep(action="Confirm the reward badge shows", case_id=12),
                ],
            ),
            ScenarioPlan(
                title="Shop purchase flow",
                description="Verify the shop purchase flow.",
                steps=[
                    AuthoredStep(action="Buy an item and confirm gold drops", case_id=21)
                ],
            ),
        ],
    )


def _canned_factory(result: ScenarioAgentResult):
    """A factory whose agent returns the graph state create_agent would produce.

    create_agent's final state carries the parsed schema under
    ``structured_response``; the agent reads exactly that key.
    """

    def factory(*, model, tools, system_prompt):
        return RunnableLambda(
            lambda _inputs: {"messages": [], "structured_response": result}
        )

    return factory


def _channel() -> ScenarioChannel:
    async def send(_frame: dict) -> None:
        return None

    return ScenarioChannel(send)


def _request(**overrides) -> ScenarioAgentRequest:
    base = {
        "user_input": "Author scenarios for login rewards and the shop.",
        "game_context": {"constraints": ["Reward can be claimed once per day."]},
        "unity_context": {"states": [{"key": "login_reward.claimed"}]},
    }
    base.update(overrides)
    return ScenarioAgentRequest(**base)


_CTX = AgentContext(session_id="session-1")


def test_agent_context_builds_trace_config() -> None:
    context = AgentContext(
        session_id="session-1", metadata={"environment": "test", "session_id": "wrong"}
    )

    assert context.trace_config("scenario-generation") == {
        "run_name": "scenario-generation",
        "tags": ["agent"],
        "metadata": {"environment": "test", "session_id": "session-1"},
    }


def test_scenario_agent_names_and_tags_its_trace() -> None:
    """The run LangSmith shows carries the name, tag and session id."""
    agent = ScenarioAgent(agent_factory=_canned_factory(_result()))

    with collect_runs() as collected:
        asyncio.run(agent.run(_request(), _CTX, _channel()))

    (root,) = collected.traced_runs
    assert root.name == "scenario-generation"
    assert root.tags == ["agent"]
    assert root.extra["metadata"]["session_id"] == "session-1"


def test_scenario_agent_returns_multi_scenario_result() -> None:
    result = _result()
    agent = ScenarioAgent(agent_factory=_canned_factory(result))

    out = asyncio.run(agent.run(_request(), _CTX, _channel()))

    assert isinstance(out, ScenarioAgentResult)
    assert out.message == "Authored two scenarios."
    assert [plan.title for plan in out.scenarios] == [
        "Login reward flow",
        "Shop purchase flow",
    ]
    assert [step.case_id for step in out.scenarios[0].steps] == [11, 11, None, 12]


def test_scenario_agent_raises_when_no_structured_response() -> None:
    """A loop that ended without a plan is a generation failure, not empty output."""

    def factory(*, model, tools, system_prompt):
        return RunnableLambda(lambda _inputs: {"messages": [], "structured_response": None})

    agent = ScenarioAgent(agent_factory=factory)

    with pytest.raises(ScenarioGenerationError):
        asyncio.run(agent.run(_request(), _CTX, _channel()))


def test_scenario_agent_binds_the_search_tool() -> None:
    """The turn is a tool loop: the case-search tool must reach the model."""
    seen: dict[str, list[str]] = {}

    def factory(*, model, tools, system_prompt):
        seen["tools"] = [tool.name for tool in tools]
        seen["system_prompt"] = system_prompt
        return RunnableLambda(
            lambda _inputs: {"messages": [], "structured_response": _result()}
        )

    agent = ScenarioAgent(agent_factory=factory)
    asyncio.run(agent.run(_request(), _CTX, _channel()))

    # 목록이 없으면 검색으로 폴백한다. 미커버 도구는 목록 유무와 무관하게 늘 붙는다 —
    # 커버 상태는 목록이 답할 수 없는 질문이고 대화 중에 바뀐다.
    assert seen["tools"] == ["list_uncovered_cases", "search_test_cases", "find_path", "explain_case"]
    # The system prompt is the resolved v4 text, every placeholder substituted.
    assert "search_test_cases" in seen["system_prompt"]
    assert "{" not in seen["system_prompt"]


def test_empty_scenarios_is_a_valid_result() -> None:
    """No matching cases: the agent returns a message and an empty plan list."""
    result = ScenarioAgentResult(message="No matching cases yet.", scenarios=[])
    agent = ScenarioAgent(agent_factory=_canned_factory(result))

    out = asyncio.run(agent.run(_request(), _CTX, _channel()))

    assert out.scenarios == []
    assert out.message == "No matching cases yet."


def test_scenario_result_parses_string_case_id_as_int() -> None:
    """Search hits carry string ids; a step's case_id stores them as ints (spec)."""
    parsed = ScenarioAgentResult.model_validate(
        {
            "message": "ok",
            "scenarios": [
                {
                    "title": "t",
                    "description": "d",
                    "steps": [
                        {"action": "do", "case_id": "7"},
                        {"action": "verify", "case_id": "8"},
                    ],
                }
            ],
        }
    )

    assert [step.case_id for step in parsed.scenarios[0].steps] == [7, 8]


def test_scenario_result_scenario_id_edit_vs_add() -> None:
    """scenario_id: an id (string coerced to int) means edit; absent means add."""
    parsed = ScenarioAgentResult.model_validate(
        {
            "message": "ok",
            "scenarios": [
                {
                    "scenario_id": "5",
                    "title": "edit",
                    "description": "d",
                    "steps": [{"action": "a", "case_id": 1}],
                },
                {"title": "add", "description": "d", "steps": [{"action": "b", "case_id": 2}]},
            ],
        }
    )

    assert parsed.scenarios[0].scenario_id == 5
    assert parsed.scenarios[1].scenario_id is None


def test_first_message_carries_current_scenarios() -> None:
    """The run's existing scenarios reach the prompt so the agent can edit by id."""
    message = build_first_message(
        _request(
            current_scenarios=[
                ScenarioPlan(
                    scenario_id=42,
                    title="Checkout flow",
                    description="Verify checkout.",
                    steps=[
                        AuthoredStep(action="Add to cart", case_id=1),
                        AuthoredStep(action="Confirm checkout succeeds", case_id=2),
                    ],
                )
            ]
        )
    )

    assert "Checkout flow" in message
    assert "42" in message
    # The placeholder was resolved (not left literal) in the rendered prompt.
    assert "{current_scenarios}" not in message


def test_first_message_empty_current_scenarios_renders_empty_list() -> None:
    """A fresh run renders the current-scenarios block as an empty JSON list."""
    message = build_first_message(_request())

    assert "[]" in message


def test_system_prompt_uses_requested_language_directive() -> None:
    ko_body, _ = build_system_prompt(_request(locale=OutputLanguage.ko))
    en_body, version = build_system_prompt(_request(locale=OutputLanguage.en))

    assert LANGUAGE_DIRECTIVES[OutputLanguage.ko] in ko_body
    assert LANGUAGE_DIRECTIVES[OutputLanguage.en] in en_body
    assert "한국어" in ko_body
    assert "English" in en_body
    # v7 is the newest scenario prompt version and the default.
    assert version == "v7"


def test_first_message_carries_the_run_goal_and_context() -> None:
    message = build_first_message(_request())

    assert "Author scenarios for login rewards and the shop." in message
    assert "login_reward.claimed" in message


def test_language_directives_cover_every_language() -> None:
    assert set(LANGUAGE_DIRECTIVES) == set(OutputLanguage)


def test_select_structured_method_by_model() -> None:
    assert select_structured_method(LLMModel.gpt_5_6_luna) == "json_schema"
    assert select_structured_method(LLMModel.gemma_4_free) == "json_mode"


# ── The test case list (ARTEL-319) ─────────────────────────────────────────────
#
# Three promises, and none of them fails loudly when broken. A turn with the list
# that still gets the search tool just spends turns re-finding what it holds; a
# render that reorders costs the prompt cache and nothing else; a clipped body
# produces a plausible wrong step. So each one is pinned here.


def _test_case_list() -> list[TestCaseListItem]:
    return [
        TestCaseListItem(
            id=11,
            scene="로그인",
            step="게스트 계정으로 로그인에 성공한다",
            precondition="앱을 최초 실행한 상태",
            expected_value="임시 계정이 발급되고 로비로 진입한다",
            verification_status="VERIFIED",
        ),
        TestCaseListItem(
            id=57,
            scene="스테이지",
            step="힌트를 쓰면 글자 하나가 공개된다",
            precondition=None,
            expected_value="보유 수량이 1 줄고 글자가 표시된다",
            verification_status="DRAFT",
        ),
    ]


def test_turn_with_the_list_gets_no_tools_and_the_cases_in_its_prompt() -> None:
    """With the cases in context, a search could only return what it already has."""
    seen: dict[str, object] = {}

    def factory(*, model, tools, system_prompt):
        seen["tools"] = [tool.name for tool in tools]
        seen["system_prompt"] = system_prompt
        return RunnableLambda(
            lambda _inputs: {"messages": [], "structured_response": _result()}
        )

    agent = ScenarioAgent(agent_factory=factory)
    asyncio.run(agent.run(_request(test_case_list=_test_case_list()), _CTX, _channel()))

    # 검색은 회수한다(목록을 이미 쥐고 있으므로). 미커버 도구는 남는다.
    assert seen["tools"] == ["list_uncovered_cases", "find_path", "explain_case"]
    prompt = seen["system_prompt"]
    assert "id 11" in prompt and "id 57" in prompt
    assert "게스트 계정으로 로그인에 성공한다" in prompt
    assert NO_TEST_CASE_LIST_NOTICE not in prompt


def test_empty_test_case_list_keeps_the_search_path() -> None:
    """The fallback is also the rollback: orchestration can stop sending one."""
    seen: dict[str, object] = {}

    def factory(*, model, tools, system_prompt):
        seen["tools"] = [tool.name for tool in tools]
        seen["system_prompt"] = system_prompt
        return RunnableLambda(
            lambda _inputs: {"messages": [], "structured_response": _result()}
        )

    agent = ScenarioAgent(agent_factory=factory)
    asyncio.run(agent.run(_request(test_case_list=[]), _CTX, _channel()))

    assert seen["tools"] == ["list_uncovered_cases", "search_test_cases", "find_path", "explain_case"]
    # Not a blank section: an empty block reads as "this project has no cases",
    # and the agent would stop rather than search.
    assert NO_TEST_CASE_LIST_NOTICE in seen["system_prompt"]


def test_render_test_case_list_preserves_arrival_order() -> None:
    """Orchestration sorts by id; re-sorting here would move the cached prefix."""
    reversed_entries = list(reversed(_test_case_list()))
    rendered = render_test_case_list(reversed_entries)

    assert rendered.index("id 57") < rendered.index("id 11")


def test_render_test_case_list_prints_bodies_whole() -> None:
    """Unlike a search hit, these are the material steps are written from."""
    long_expected = "가" * 900
    rendered = render_test_case_list(
        [
            TestCaseListItem(
                id=1,
                scene="상점",
                step="구매",
                precondition="보석 100개 이상",
                expected_value=long_expected,
                verification_status="VERIFIED",
            )
        ]
    )

    assert long_expected in rendered
    assert "truncated" not in rendered
    assert "precondition: 보석 100개 이상" in rendered


# ── Authoring without cases on explicit request (v6) ──────────────────────────
#
# The unlock is prompt-only, so what is pinned here is the prompt text and the
# shape it asks for. Whether a given model honours it is not testable offline;
# what is testable is that the default rule survived beside it, that the unlock
# names the null `case_id`, and that a result made entirely of ungrounded steps
# is a valid one to hand downstream.


def test_v6_unlocks_caseless_authoring_and_keeps_the_default_rule() -> None:
    v6 = load_prompt("scenario", "system", "v6").body
    v5 = load_prompt("scenario", "system", "v5").body

    # The defaults are unchanged — the unlock is an exception, not a replacement.
    assert "**Do NOT invent.**" in v6
    assert "Ground every step in the cases above." in v6
    # v5's exhaustive in/out verdict survives too, and the unlock says which side
    # an uncovered case lands on: `in` is a promise some step carries the id.
    assert "judge EVERY case in the list above" in v6
    assert "no id may go to `reviewed.in` on its account" in v6
    # An ungrounded step carries no id: guessing one would file a verdict against
    # a case the step never exercises.
    assert "Leave `case_id` null on EVERY step" in v6
    # v5 has no such section, which is the whole reason v6 exists.
    assert "WITHOUT TEST CASES" not in v5


def test_a_scenario_of_ungrounded_steps_is_a_valid_result() -> None:
    """All-null `case_id` survives the contract: orchestration stores steps whole
    and resolves a `case` only where an id is present."""
    plan = ScenarioPlan(
        title="튜토리얼 첫 진입",
        description="케이스 없이 작성한 흐름 초안.",
        steps=[
            AuthoredStep(action="게임을 처음 실행한다"),
            AuthoredStep(action="시작 버튼을 누른다"),
            AuthoredStep(action="튜토리얼 첫 대사가 뜨는지 확인한다"),
        ],
    )
    result = ScenarioAgentResult(
        message="기존 테스트 케이스와 연결되지 않은 초안이에요.", scenarios=[plan]
    )

    agent = ScenarioAgent(agent_factory=_canned_factory(result))
    returned = asyncio.run(agent.run(_request(), _CTX, _channel()))

    assert [step.case_id for step in returned.scenarios[0].steps] == [None, None, None]


# ── 흐름 단위 분할과 브리지 (ARTEL-420, v7) ───────────────────────────────────
#
# 분할 품질은 오프라인에서 셀 수 없다. 셀 수 있는 것은 기준이 프롬프트에 하나로
# 적혀 있는지, 그리고 v6이 들고 있던 규칙이 그 옆에서 살아남았는지다.


def test_v7_pins_splitting_to_reachability_and_keeps_v6_intact() -> None:
    v7 = load_prompt("scenario", "system", "v7").body
    v6 = load_prompt("scenario", "system", "v6").body

    # 나누느냐 잇느냐를 도달 가능성 하나로 환원한다.
    assert "it is the NEXT STEP, not the next scenario" in v7
    assert "two that exclude each other do not" in v7
    # 이미 덮인 구간은 지나가되 다시 세지 않는다 — 앞부분 통째 복사를 막는 규칙.
    assert "Leave their `case_id` null" in v7
    assert "Coverage counts a case once it is carried anywhere" in v7
    # v6의 케이스 없는 저작 예외와 근거 규칙은 그대로 남는다.
    assert "WHEN THE USER ASKS FOR A SCENARIO WITHOUT TEST CASES" in v7
    assert "**Do NOT invent.**" in v7
    assert "judge EVERY case in the list above" in v7
    # v6에는 기준이 "흐름이 여럿이면 나눠라" 한 줄뿐이라 요청 하나가 흐름 하나로 읽혔다.
    assert "it is the NEXT STEP, not the next scenario" not in v6


def test_system_prompt_with_the_list_is_byte_identical_across_turns() -> None:
    """The prompt cache only pays off while this block does not move."""
    request = _request(test_case_list=_test_case_list())
    first, _ = build_system_prompt(request)
    second, _ = build_system_prompt(request)

    assert first == second
    # And a placeholder left unsubstituted would silently ship "{test_case_list}".
    assert "{" not in first


# --- 전 건 판정 (ARTEL-404) ---------------------------------------------------


def test_reviewed_uses_in_and_out_on_the_wire() -> None:
    """와이어 키는 `in`/`out`이다.

    `in`이 파이썬 예약어라 필드명은 `included`/`excluded`이고 별칭으로 내보낸다. 이 별칭이
    빠지면 orche가 판정을 못 읽고, 못 읽으면 **검사가 조용히 꺼진다** — 실패가 아니라 무동작으로
    나타나므로 눈에 띄지 않는다.
    """
    reviewed = ReviewedCases(included=[1, 2], excluded=[3])

    assert reviewed.model_dump(by_alias=True) == {"in": [1, 2], "out": [3]}


def test_reviewed_parses_from_wire_names() -> None:
    assert ReviewedCases(**{"in": [7], "out": [8]}).included == [7]


def test_result_without_reviewed_is_none_not_empty() -> None:
    """판정이 없을 때 빈 객체가 아니라 None이어야 한다.

    orche는 None을 "검사 건너뜀"으로 읽는다. 빈 객체를 보내면 "전량을 판정했는데 하나도 안
    골랐다"가 되어, 케이스가 있는 프로젝트에서는 전량이 검토 누락으로 잡힌다.
    """
    assert ScenarioAgentResult(message="…").reviewed is None


def test_uncovered_is_a_tool_not_a_prompt_block() -> None:
    """미커버는 프롬프트에 싣지 않는다.

    저작할수록 줄어드는 값이라 세션 오픈 스냅샷은 둘째 턴부터 틀리고, 매 턴 다시 실으면 턴
    메시지가 붓거나(더 나쁘게는 system에 있으면) 전량 목록 캐시를 통째로 버린다. 물어볼 때만
    내는 도구가 맞다.
    """
    body, _ = build_system_prompt(_request())

    assert "list_uncovered_cases" in body
    assert "{uncovered_case_ids}" not in body
