# -*- coding: utf-8 -*-
"""입구 라우터(워크플로 재편 Step 1).

갈래별로 읽을 만큼만 읽는다 — 인사·가드레일은 모델 없이, 질문은 케이스 전량 없이
(설계된 검색 폴백을 태워서), 저작·미분류는 현행 루프 그대로. 라우터가 없으면(기본값)
예전과 완전히 같아야 한다 — 그 동등성이 롤백 스위치다.
"""

import asyncio

import pytest
from langchain_core.runnables import RunnableLambda

from app.agents.scenario.agent import ScenarioAgent
from app.agents.scenario.cases import NO_TEST_CASE_LIST_NOTICE
from app.agents.scenario.router import Route, RouteVerdict, ScenarioRouter
from app.agents.scenario.schemas import TestCaseListItem
from tests.test_agents_scenario import _CTX, _channel, _request, _result


def _case_list() -> list[TestCaseListItem]:
    return [TestCaseListItem(
        id=1, scene="Shop", step="상점을 연다", expected_value="상점이 열린다",
        verification_status="VERIFIED",
    )]


def _fixed_router(route: Route, reply: str = "") -> ScenarioRouter:
    async def caller(prompt: str) -> RouteVerdict:
        assert "User message:" in prompt  # 프롬프트 조립이 살아 있는지까지 겸사겸사
        return RouteVerdict(route=route, reply=reply)

    return ScenarioRouter(caller=caller)


def _capturing_factory(seen: dict):
    def factory(*, model, tools, system_prompt, reasoning=None):
        seen["tools"] = [tool.name for tool in tools]
        seen["system_prompt"] = system_prompt
        return RunnableLambda(lambda _inputs: {"messages": [], "structured_response": _result()})

    return factory


def test_greeting_ends_without_any_model_loop() -> None:
    seen: dict = {}
    agent = ScenarioAgent(
        agent_factory=_capturing_factory(seen),
        router=_fixed_router(Route.GREETING, reply="안녕하세요!"),
    )

    out = asyncio.run(agent.run(_request(user_input="안녕"), _CTX, _channel()))

    assert out.message == "안녕하세요!"
    assert out.scenarios == []
    assert "system_prompt" not in seen  # 루프가 아예 안 돌았다 — 74k 를 안 읽었다


def test_offtopic_gets_a_polite_decline() -> None:
    agent = ScenarioAgent(
        agent_factory=_capturing_factory({}),
        router=_fixed_router(Route.OFFTOPIC),
    )

    out = asyncio.run(agent.run(_request(user_input="오늘 날씨 어때"), _CTX, _channel()))

    assert out.scenarios == []
    assert out.message  # 라우터가 문구를 안 만들면 기본 문구가 나간다


def test_question_reenters_slim_on_the_search_fallback() -> None:
    """질문 갈래는 새 기계가 아니다 — 전량을 비우면 검색 폴백(ARTEL-319 롤백 경로)이
    켜진다. 소형 프롬프트 + 검색 도구가 그 증거다."""
    seen: dict = {}
    agent = ScenarioAgent(
        agent_factory=_capturing_factory(seen),
        router=_fixed_router(Route.QUESTION),
    )

    asyncio.run(agent.run(_request(user_input="결제 케이스 있어?"), _CTX, _channel()))

    assert NO_TEST_CASE_LIST_NOTICE.splitlines()[0] in seen["system_prompt"]
    assert "search_test_cases" in seen["tools"]


def test_workflow_off_falls_back_to_the_loop_with_full_context(monkeypatch) -> None:
    """롤백 스위치: 워크플로를 끄면 저작이 현행 루프로 — 케이스 전량이 프롬프트에 있다."""
    import app.agents.scenario.agent as agent_mod

    class _Off:
        scenario_workflow_enabled = False
        scenario_loop_enabled = True
        scenario_max_model_calls = 40
        scenario_max_tool_calls = 30

    monkeypatch.setattr(agent_mod, "get_settings", lambda: _Off())
    seen: dict = {}
    agent = ScenarioAgent(
        agent_factory=_capturing_factory(seen),
        router=_fixed_router(Route.AUTHORING),
    )

    asyncio.run(agent.run(
        _request(user_input="상점 흐름 짜줘", test_case_list=_case_list()), _CTX, _channel(),
    ))

    assert NO_TEST_CASE_LIST_NOTICE.splitlines()[0] not in seen["system_prompt"]


def test_router_failure_falls_back_to_authoring() -> None:
    async def broken(prompt: str) -> RouteVerdict:
        raise RuntimeError("router down")

    verdict = asyncio.run(ScenarioRouter(caller=broken).route("아무 말"))

    assert verdict.route is Route.AUTHORING


def test_no_router_means_the_old_behaviour() -> None:
    seen: dict = {}
    agent = ScenarioAgent(agent_factory=_capturing_factory(seen))

    out = asyncio.run(agent.run(_request(user_input="안녕"), _CTX, _channel()))

    # 라우터가 없으면 인사도 루프로 간다 — 이 동등성이 롤백 스위치다.
    assert "system_prompt" in seen
    assert out.scenarios


def test_limit_middleware_carries_the_settings_caps() -> None:
    """폭주 방지선(⑥)은 코드가 든다 — 미들웨어가 설정값 그대로의 한도를 들고 붙는지."""
    from app.agents.scenario.agent import limit_middleware
    from app.config import get_settings

    model_cap, tool_cap = limit_middleware()

    assert model_cap.run_limit == get_settings().scenario_max_model_calls
    assert model_cap.exit_behavior == "end"  # 죽지 않고 끝나야 회수 장치가 기록을 건진다
    assert tool_cap.run_limit == get_settings().scenario_max_tool_calls


class _FakeChannel:
    def __init__(self, answers=None):
        self.submitted = []
        self.stages = []
        self._answers = answers or []

    async def report(self, stage: str) -> None:
        self.stages.append(stage)

    async def submit_scenario(self, scenario: dict):
        from app.sessions.channel import ScenarioAccepted
        self.submitted.append(scenario)
        if self._answers:
            return self._answers.pop(0)
        return ScenarioAccepted(accepted=True, written=len(self.submitted))


def _workflow_agent(monkeypatch, plan, writers, plans=None, scores=None):
    """B·C 호출과 채점 창구를 가짜로 갈아 끼운 워크플로 — 모델 없이 배선만 검사한다.

    `plans` 는 B 재호출(걷기 검증 재작성)이 받을 다음 계획들, `scores` 는
    `_score_groups` 가 차례로 낼 답(None=검증 불가, []=깨끗함, [문장…]=어긋남).
    기본은 검증 불가 — 채점 창구가 없는 옛 환경과 같은 동작이다.
    """
    import app.agents.scenario.workflow as wf

    calls = {"b": 0, "c": [], "b_prompts": []}
    b_plans = [plan, *(plans or [])]
    score_answers = list(scores or [])

    async def fake_call(model, reasoning, schema, prompt):
        prefix, tail = prompt  # (prefix, tail) 계약 — 캐시 경계가 그 사이에 앉는다
        if schema is wf.GroupingPlan:
            calls["b"] += 1
            calls["b_prompts"].append(f"{prefix}\n{tail}")
            return b_plans.pop(0)
        calls["c"].append(f"{prefix}\n{tail}")
        return writers.pop(0)

    async def fake_score(request, groups):
        return score_answers.pop(0) if score_answers else None

    monkeypatch.setattr(wf, "_call", fake_call)
    monkeypatch.setattr(wf, "_score_groups", fake_score)
    return wf, calls


def _case_step(case_id: int = 1):
    from app.agents.scenario.schemas import AuthoredStep
    return AuthoredStep(action="상점을 열어 확인한다", case_id=case_id, step_source="CASE")


def test_workflow_groups_writes_and_submits_in_order(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="첫 여정", case_ids=[1]),
        wf_mod.Group(title="둘째 여정", case_ids=[1]),
    ], note="좁게 읽음")
    writers = [wf_mod._Writer(steps=[_case_step()]), wf_mod._Writer(steps=[_case_step()])]
    wf, calls = _workflow_agent(monkeypatch, plan, writers)
    channel = _FakeChannel()

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))

    assert calls["b"] == 1
    assert [s["title"] for s in channel.submitted] == ["첫 여정", "둘째 여정"]  # 순서 보존
    # 노드 경계 보고: B 앞에 thinking 한 번, worker 마다 writing — 침묵이 안 생긴다.
    assert channel.stages == ["thinking", "writing", "writing"]
    # 판정은 B 의 선택에서 결정적으로 — 모델을 다시 안 부른다.
    assert out.reviewed is not None and out.reviewed.included == [1]
    assert out.scenarios == []  # 저장은 제출로 끝났다 — 봉투에 다시 실으면 두 벌이 된다
    assert "2개 시나리오" in out.message and "좁게 읽음" in out.message


def test_workflow_question_returns_early(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[], question="엔딩 전이 보스 포함인가요?")
    wf, calls = _workflow_agent(monkeypatch, plan, [])
    channel = _FakeChannel()

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="애매한 요청", test_case_list=_case_list()), _CTX, channel,
    ))

    assert out.message == "엔딩 전이 보스 포함인가요?"
    assert channel.submitted == [] and not calls["c"]


def test_workflow_retries_a_declined_group_once_then_drops(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod
    from app.sessions.channel import ScenarioAccepted

    plan = wf_mod.GroupingPlan(groups=[wf_mod.Group(title="여정", case_ids=[1])])
    # 원본 + 재작성 — 케이스가 실려 있어 미탑재 보수는 안 돈다.
    writers = [wf_mod._Writer(steps=[_case_step()]), wf_mod._Writer(steps=[_case_step()])]
    wf, calls = _workflow_agent(monkeypatch, plan, writers)
    channel = _FakeChannel(answers=[
        ScenarioAccepted(accepted=False, detail="3번째 스텝: 근거가 어긋납니다"),
        ScenarioAccepted(accepted=False, detail="같은 이유"),
    ])

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))

    assert len(channel.submitted) == 2          # 원본 + 재시도 한 번, 그 뒤엔 버림
    assert "근거가 어긋납니다" in calls["c"][1]  # 반려 이유가 worker 에게 전달됐다
    assert "0개 시나리오" in out.message and "여정" in out.message
    # 저장된 것이 없으니 판정도 없다 — in 은 저장된 스텝에서만 나온다.
    assert out.reviewed is not None and out.reviewed.included == []


def test_workflow_repairs_an_unplaced_case_once(monkeypatch) -> None:
    """B 가 묶은 케이스를 C 가 스텝에 안 실으면 그 worker 가 1회 보수한다 — 여기서 새면
    턴끝 검수가 막고 구 루프(전량 프롬프트)가 불려 나간다(run 47 실측 그 자리)."""
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[wf_mod.Group(title="여정", case_ids=[1])])
    writers = [wf_mod._Writer(steps=[]), wf_mod._Writer(steps=[_case_step()])]
    wf, calls = _workflow_agent(monkeypatch, plan, writers)
    channel = _FakeChannel()

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))

    assert len(calls["c"]) == 2 and "case_id" in calls["c"][1]  # 보수 딱 한 번
    assert out.reviewed is not None and out.reviewed.included == [1]
    assert "싣지 못해" not in out.message  # 보수로 채워졌으니 사과문도 없다


def test_workflow_reports_a_case_it_could_not_place(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[wf_mod.Group(title="여정", case_ids=[1])])
    writers = [wf_mod._Writer(steps=[]), wf_mod._Writer(steps=[])]  # 보수까지 빈손
    wf, calls = _workflow_agent(monkeypatch, plan, writers)
    channel = _FakeChannel()

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))

    # in 으로 거짓말하지 않는다 — out 으로 보내고 문구에 밝힌다.
    assert out.reviewed is not None and out.reviewed.included == []
    assert out.reviewed.excluded == [1]
    assert "싣지 못해" in out.message


def test_workflow_merges_into_an_existing_scenario_when_b_points_at_it(monkeypatch) -> None:
    """거의 같은 요청이 중복 행을 만들지 않는다(run 57, 558 vs 560 실측). B 가 기존
    시나리오 번호를 가리키면 그 본문을 교체하도록 scenario_id 가 제출까지 흐른다."""
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="게임 시작 여정", case_ids=[1], scenario_id=560),
    ])
    writers = [wf_mod._Writer(steps=[_case_step()])]
    wf, calls = _workflow_agent(monkeypatch, plan, writers)
    channel = _FakeChannel()

    aio.run(wf.run_authoring_workflow(
        _request(
            user_input="같은 범위 다시 짜줘",
            test_case_list=_case_list(),
            current_scenarios=[_current_scenario(scenario_id=560, title="게임 시작 여정")],
        ),
        _CTX, channel,
    ))

    assert channel.submitted[0]["scenario_id"] == 560   # 새 행이 아니라 교체
    # B 는 기존 목록을 tail 에서 읽는다 — 캐시되는 prefix 를 더럽히지 않는다.
    assert "EXISTING SCENARIOS" in calls["b_prompts"][0]
    assert "[id 560]" in calls["b_prompts"][0]


def test_workflow_ignores_a_ghost_merge_target(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="여정", case_ids=[1], scenario_id=999),  # 목록에 없는 번호
    ])
    writers = [wf_mod._Writer(steps=[_case_step()])]
    wf, _ = _workflow_agent(monkeypatch, plan, writers)
    channel = _FakeChannel()

    aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list(),
                 current_scenarios=[_current_scenario(scenario_id=560)]),
        _CTX, channel,
    ))

    assert channel.submitted[0]["scenario_id"] is None  # 짐작으로 남의 행을 안 덮는다


def test_workflow_b_regroups_once_on_order_findings(monkeypatch) -> None:
    """B 미니 루프: 걷기 검증이 어긋남을 짚으면 B 가 그 지적을 들고 한 번 다시 묶는다.
    run 53 의 그 부류(상태 조건이 안 맞는 케이스 배치)를 문장 쓰기 전에 잡는 자리다."""
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    first = wf_mod.GroupingPlan(groups=[wf_mod.Group(title="어긋난 여정", case_ids=[1])])
    fixed = wf_mod.GroupingPlan(groups=[wf_mod.Group(title="고친 여정", case_ids=[1])])
    writers = [wf_mod._Writer(steps=[_case_step()])]
    wf, calls = _workflow_agent(
        monkeypatch, first, writers, plans=[fixed],
        scores=[["여정: 17번째 스텝이 StagePosition <= 0 를 요구하는데"], []],
    )
    channel = _FakeChannel()

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))

    assert calls["b"] == 2                                # 재작성 딱 한 번
    assert "StagePosition" in calls["b_prompts"][1]       # 지적이 B 에게 전달됐다
    assert channel.submitted[0]["title"] == "고친 여정"    # 고친 묶음으로 진행
    assert "1개 시나리오" in out.message


def test_workflow_b_skips_verification_without_scope(monkeypatch) -> None:
    """채점 창구를 못 쓰는 환경(스코프 없음·창구 다운)에서는 검증 없이 그대로 진행한다."""
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plan = wf_mod.GroupingPlan(groups=[wf_mod.Group(title="여정", case_ids=[1])])
    writers = [wf_mod._Writer(steps=[_case_step()])]
    wf, calls = _workflow_agent(monkeypatch, plan, writers, scores=[None])
    channel = _FakeChannel()

    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))

    assert calls["b"] == 1 and "1개 시나리오" in out.message


def _current_scenario(scenario_id: int = 7, title: str = "상점 여정"):
    from app.agents.scenario.schemas import ScenarioPlan
    return ScenarioPlan(
        scenario_id=scenario_id, title=title, description="상점을 검증한다",
        steps=[_case_step()],
    )


def _modify_agent(monkeypatch, plans):
    """E 의 모델 호출을 가짜로 갈아 끼운다 — 배선만 검사한다."""
    import app.agents.scenario.workflow as wf

    calls: list[str] = []

    async def fake_call(model, reasoning, schema, prompt):
        assert schema is wf.ModifyPlan
        prefix, tail = prompt
        calls.append(f"{prefix}\n{tail}")
        return plans.pop(0)

    monkeypatch.setattr(wf, "_call", fake_call)
    return wf, calls


def test_modify_workflow_replaces_the_target_by_id(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plans = [wf_mod.ModifyPlan(
        scenario_id=7, title="", description="", steps=[_case_step()], note="스텝을 다듬음",
    )]
    wf, calls = _modify_agent(monkeypatch, plans)
    channel = _FakeChannel()

    out = aio.run(wf.run_modify_workflow(
        _request(
            user_input="상점 여정 3번 스텝 고쳐줘",
            test_case_list=_case_list(),
            current_scenarios=[_current_scenario()],
        ),
        _CTX, channel,
    ))

    assert len(calls) == 1 and "CURRENT SCENARIOS" in calls[0]
    submitted = channel.submitted[0]
    assert submitted["scenario_id"] == 7
    assert submitted["title"] == "상점 여정"  # 제목을 안 냈으면 원본 제목이 산다
    assert "수정해 저장했습니다" in out.message and out.scenarios == []
    assert out.reviewed is None  # 수정은 전 건 판정 턴이 아니다


def test_modify_workflow_asks_when_the_target_is_unclear(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plans = [wf_mod.ModifyPlan(question="어느 시나리오를 고칠까요?")]
    wf, _ = _modify_agent(monkeypatch, plans)
    channel = _FakeChannel()

    out = aio.run(wf.run_modify_workflow(
        _request(
            user_input="아까 그거 고쳐줘", test_case_list=_case_list(),
            current_scenarios=[_current_scenario()],
        ),
        _CTX, channel,
    ))

    assert out.message == "어느 시나리오를 고칠까요?" and channel.submitted == []


def test_modify_workflow_refuses_a_ghost_scenario_id(monkeypatch) -> None:
    """모델이 목록에 없는 id 를 가리키면 짐작으로 갈아끼우지 않는다 — 묻는다."""
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod

    plans = [wf_mod.ModifyPlan(scenario_id=999, steps=[_case_step()])]
    wf, _ = _modify_agent(monkeypatch, plans)
    channel = _FakeChannel()

    out = aio.run(wf.run_modify_workflow(
        _request(
            user_input="고쳐줘", test_case_list=_case_list(),
            current_scenarios=[_current_scenario()],
        ),
        _CTX, channel,
    ))

    assert channel.submitted == []
    assert "상점 여정(id 7)" in out.message and "저장하지 않았습니다" in out.message


def test_modify_workflow_retries_once_then_leaves_the_original(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.workflow as wf_mod
    from app.sessions.channel import ScenarioAccepted

    plans = [
        wf_mod.ModifyPlan(scenario_id=7, steps=[_case_step()]),
        wf_mod.ModifyPlan(scenario_id=7, steps=[_case_step()]),  # 재작성
    ]
    wf, calls = _modify_agent(monkeypatch, plans)
    channel = _FakeChannel(answers=[
        ScenarioAccepted(accepted=False, detail="2번째 스텝: 근거가 어긋납니다"),
        ScenarioAccepted(accepted=False, detail="같은 이유"),
    ])

    out = aio.run(wf.run_modify_workflow(
        _request(
            user_input="고쳐줘", test_case_list=_case_list(),
            current_scenarios=[_current_scenario()],
        ),
        _CTX, channel,
    ))

    assert len(channel.submitted) == 2 and "근거가 어긋납니다" in calls[1]
    assert "기존 본문이 그대로" in out.message


def test_authoring_route_enters_the_workflow(monkeypatch) -> None:
    import asyncio as aio
    import app.agents.scenario.agent as agent_mod

    hit = {}

    async def fake_workflow(request, context, channel):
        hit["request"] = request
        from app.agents.scenario.schemas import ScenarioAgentResult
        return ScenarioAgentResult(message="워크플로", scenarios=[])

    monkeypatch.setattr(agent_mod, "run_authoring_workflow", fake_workflow)
    agent = ScenarioAgent(
        agent_factory=_capturing_factory({}), router=_fixed_router(Route.AUTHORING),
    )

    out = aio.run(agent.run(
        _request(user_input="상점 흐름 짜줘", test_case_list=_case_list()), _CTX, _channel(),
    ))

    assert out.message == "워크플로" and "request" in hit


def test_modify_route_enters_the_edit_workflow(monkeypatch) -> None:
    """Step 3: MODIFY 는 이제 E 갈래로 간다 — 루프도, 끊김도 아니다."""
    import asyncio as aio
    import app.agents.scenario.agent as agent_mod

    class _ServiceDefaults:
        scenario_workflow_enabled = True
        scenario_loop_enabled = True
        scenario_max_model_calls = 40
        scenario_max_tool_calls = 30

    monkeypatch.setattr(agent_mod, "get_settings", lambda: _ServiceDefaults())
    hit = {}

    async def fake_modify(request, context, channel):
        hit["request"] = request
        from app.agents.scenario.schemas import ScenarioAgentResult
        return ScenarioAgentResult(message="수정 갈래", scenarios=[])

    monkeypatch.setattr(agent_mod, "run_modify_workflow", fake_modify)
    seen: dict = {}
    agent = ScenarioAgent(
        agent_factory=_capturing_factory(seen), router=_fixed_router(Route.MODIFY),
    )

    out = aio.run(agent.run(
        _request(user_input="3번 시나리오 고쳐줘", test_case_list=_case_list()), _CTX, _channel(),
    ))

    assert out.message == "수정 갈래" and "request" in hit
    assert "system_prompt" not in seen  # 루프는 안 돌았다


def test_modify_route_falls_back_to_the_loop_when_workflow_is_off(monkeypatch) -> None:
    """롤백 스위치: 워크플로를 끄면 수정도 예전처럼 루프가 받는다(루프는 켜진 채)."""
    import asyncio as aio
    import app.agents.scenario.agent as agent_mod

    class _WorkflowOff:
        scenario_workflow_enabled = False
        scenario_loop_enabled = True
        scenario_max_model_calls = 40
        scenario_max_tool_calls = 30

    monkeypatch.setattr(agent_mod, "get_settings", lambda: _WorkflowOff())
    seen: dict = {}
    agent = ScenarioAgent(
        agent_factory=_capturing_factory(seen), router=_fixed_router(Route.MODIFY),
    )

    out = aio.run(agent.run(
        _request(user_input="3번 시나리오 고쳐줘", test_case_list=_case_list()), _CTX, _channel(),
    ))

    assert "system_prompt" in seen
    assert out.scenarios


def test_loop_disabled_cuts_the_modify_route_and_records(monkeypatch) -> None:
    """구 루프 끊기(실험): 루프로 갈 요청은 기록만 남기고 저작하지 않는다 — 전량
    프롬프트가 안 나간다. 서비스 기본값(scenario_loop_enabled=True)은 예전 그대로다."""
    import asyncio as aio
    import app.agents.scenario.agent as agent_mod

    # 워크플로까지 꺼야 루프행이 생긴다 — MODIFY 는 이제 E 갈래(Step 3)가 받으니까.
    class _LoopOff:
        scenario_workflow_enabled = False
        scenario_loop_enabled = False
        scenario_max_model_calls = 40
        scenario_max_tool_calls = 30

    monkeypatch.setattr(agent_mod, "get_settings", lambda: _LoopOff())
    recorded: list[tuple] = []
    monkeypatch.setattr(agent_mod.trace, "record", lambda *a: recorded.append(a))
    seen: dict = {}
    agent = ScenarioAgent(
        agent_factory=_capturing_factory(seen), router=_fixed_router(Route.MODIFY),
    )

    out = aio.run(agent.run(
        _request(user_input="3번 시나리오 고쳐줘", test_case_list=_case_list()), _CTX, _channel(),
    ))

    assert "system_prompt" not in seen  # 루프가 아예 안 돌았다 — 전량 프롬프트 0
    assert out.scenarios == [] and "기록해 두었어요" in out.message
    assert recorded and "modify" in recorded[0][2]  # 어떤 요청이었는지는 남는다
