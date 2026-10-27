# -*- coding: utf-8 -*-
"""TC 초안(ARTEL-1273): 시나리오 저작이 없는 TC 를 초안으로 제안하고, TC 만들기 갈래가 초안만 내는지.

모델은 부르지 않는다. 묶기 · 문장 · TC 만들기 호출을 가짜로 바꾸고 제출된 payload 와 답을 본다.
"""

import asyncio

import app.agents.scenario.workflow as wf
from app.agents.scenario.agent import ScenarioAgent
from app.agents.scenario.router import Route
from app.agents.scenario.schemas import AuthoredStep, ProposedCase, ScenarioAgentResult
from tests.test_agents_scenario import _CTX, _request
from tests.test_scenario_free_mode import _cases
from tests.test_scenario_router import _FakeChannel

DRAFT = ProposedCase(ref=-1, scene="Shop", action="보상을 받는다", expected="보상 카드가 손에 들어온다")


def _fake_calls(monkeypatch, answers: dict) -> None:
    async def fake_call(model, reasoning, schema, prompt):
        return answers[schema]

    async def fake_score(request, groups):
        return []

    monkeypatch.setattr(wf, "_call", fake_call)
    monkeypatch.setattr(wf, "_score_groups", fake_score)


def test_a_draft_step_goes_out_without_a_case_id_and_the_draft_is_in_the_answer(monkeypatch) -> None:
    _fake_calls(monkeypatch, {
        wf.GroupingPlan: wf.GroupingPlan(
            groups=[wf.Group(title="상점 여정", case_ids=[1, -1])], proposed_cases=[DRAFT],
        ),
        wf._Writer: wf._Writer(steps=[
            AuthoredStep(action="상점을 연다", case_id=1, step_source="CASE"),
            AuthoredStep(action="보상을 받는다", case_id=-1, step_source="CASE"),
        ]),
    })
    channel = _FakeChannel()

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="상점에서 보상 받는 것까지 짜줘", test_case_list=_cases()), _CTX, channel,
    ))

    (sent,) = channel.submitted
    assert sent["steps"][0] == {
        "action": "상점을 연다", "case_id": 1, "hint": None, "input": None, "step_source": "CASE",
        "step_source_capability_id": None, "step_unknown_reason": None,
    }
    assert sent["steps"][1]["case_id"] is None
    assert sent["steps"][1]["proposed_case"] == -1
    assert sent["steps"][1]["step_source"] == "UNKNOWN"
    assert out.proposed_cases == [DRAFT]
    assert "보상 카드가 손에 들어온다" in out.reply.detail
    # 초안은 "넣을 자리가 없던 TC" 로 되묻지 않는다.
    assert not any("넣을 자리가 없던" in ask.text for ask in out.reply.asks)
    assert out.reviewed.included == [1]


def test_a_draft_no_step_used_is_not_offered(monkeypatch) -> None:
    _fake_calls(monkeypatch, {
        wf.GroupingPlan: wf.GroupingPlan(groups=[wf.Group(title="상점 여정", case_ids=[1])], proposed_cases=[DRAFT]),
        wf._Writer: wf._Writer(steps=[AuthoredStep(action="상점을 연다", case_id=1, step_source="CASE")]),
    })

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="상점 짜줘", test_case_list=_cases()), _CTX, _FakeChannel(),
    ))

    assert out.proposed_cases == []
    assert "TC 초안" not in out.reply.detail


def test_an_unknown_negative_id_is_dropped_like_any_ghost(monkeypatch) -> None:
    plan = wf.GroupingPlan(groups=[wf.Group(title="여정", case_ids=[1, -5])], proposed_cases=[DRAFT])

    (group,) = wf._groups_of(plan, _request(user_input="x", test_case_list=_cases()))

    assert group.case_ids == [1]


def test_the_case_route_drafts_only_and_renumbers(monkeypatch) -> None:
    _fake_calls(monkeypatch, {
        wf.CasePlan: wf.CasePlan(cases=[
            ProposedCase(ref=3, scene="Shop", action="구매를 누른다", expected="골드가 줄어든다"),
            ProposedCase(ref=3, scene="Shop", action="둘러본다", expected=""),
            ProposedCase(ref=7, scene="Shop", action="", expected="가격표가 보인다"),
        ]),
    })
    channel = _FakeChannel()

    out = asyncio.run(wf.run_case_workflow(
        _request(user_input="상점 TC 만들어줘", test_case_list=_cases()), _CTX, channel,
    ))

    assert channel.submitted == []
    assert [(d.ref, d.expected) for d in out.proposed_cases] == [(-1, "골드가 줄어든다"), (-2, "가격표가 보인다")]
    assert out.reply.result == "TC 초안 2개를 만들었어요."
    assert out.scenarios == []


def test_the_case_route_goes_to_the_case_node(monkeypatch) -> None:
    async def fake_case(request, context, channel):
        return ScenarioAgentResult(message="초안", scenarios=[])

    class _Router:
        async def route(self, *args, **kwargs):
            from app.agents.scenario.router import RouteVerdict
            return RouteVerdict(route=Route.CASE)

    monkeypatch.setattr(wf, "run_case_workflow", fake_case)
    agent = ScenarioAgent(agent_factory=lambda **kwargs: None, router=_Router())

    out = asyncio.run(agent.run(_request(user_input="상점 TC 만들어줘"), _CTX, _FakeChannel()))

    assert out.message == "초안"


def test_the_first_line_is_the_models_when_everything_was_saved(monkeypatch) -> None:
    _fake_calls(monkeypatch, {
        wf.GroupingPlan: wf.GroupingPlan(result="상점 흐름을 시나리오 하나로 짰어요.", groups=[wf.Group(title="상점 여정", case_ids=[1])]),
        wf._Writer: wf._Writer(steps=[AuthoredStep(action="상점을 연다", case_id=1, step_source="CASE")]),
    })

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="상점 짜줘", test_case_list=_cases()), _CTX, _FakeChannel(),
    ))

    assert out.reply.result == "상점 흐름을 시나리오 하나로 짰어요."


def test_the_first_line_is_counted_when_a_scenario_was_rejected(monkeypatch) -> None:
    from app.sessions.channel import ScenarioAccepted

    _fake_calls(monkeypatch, {
        wf.GroupingPlan: wf.GroupingPlan(result="두 흐름을 짰어요.", groups=[
            wf.Group(title="상점 여정", case_ids=[1]), wf.Group(title="창고 여정", case_ids=[2]),
        ]),
        wf._Writer: wf._Writer(steps=[AuthoredStep(action="연다", case_id=1, step_source="CASE")]),
    })
    channel = _FakeChannel(answers=[
        ScenarioAccepted(accepted=True, written=1, steps=1),
        ScenarioAccepted(accepted=False, written=0, steps=0),
    ])

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="둘 다 짜줘", test_case_list=_cases()), _CTX, channel,
    ))

    assert out.reply.result == "총 1건의 시나리오를 생성했습니다."


def test_a_turn_that_writes_nothing_says_why_in_the_models_words(monkeypatch) -> None:
    _fake_calls(monkeypatch, {
        wf.GroupingPlan: wf.GroupingPlan(result="앞으로 의도된 막힘 확인도 넣을게요.", detail="설정으로 이해했어요."),
    })

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="앞으로는 막힘 확인도 넣어줘", test_case_list=_cases()), _CTX, _FakeChannel(),
    ))

    assert out.reply.result == "앞으로 의도된 막힘 확인도 넣을게요."
    assert "TC를 찾지 못해서" not in out.message
