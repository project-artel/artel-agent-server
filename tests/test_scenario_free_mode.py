# -*- coding: utf-8 -*-
"""FREE 시나리오(순서 없는 TC 묶음)를 저작 agent 가 만들고, FREE 인 시나리오를 FREE 로 고치는지.

모델은 부르지 않는다. 묶기·수정 모델 호출과 채점 창구를 가짜로 바꾸고, 제출된 payload 와
writer 호출 여부만 본다.
"""

import asyncio

import pytest
from langchain_core.runnables import RunnableLambda

import app.agents.scenario.workflow as wf_mod
from app.agents.scenario.agent import ScenarioAgent
from app.agents.scenario.cases import TestCaseSearchState
from app.agents.scenario.schemas import (
    AuthoredStep,
    ScenarioAgentRequest,
    ScenarioAgentResult,
    ScenarioPlan,
    TestCaseListItem,
    free_steps,
)
from app.agents.scenario.tools import build_tools
from app.sessions.channel import ScenarioAccepted
from tests.test_agents_scenario import _CTX, _channel, _request
from tests.test_scenario_ordering import CASES as CLIMB_CASES
from tests.test_scenario_ordering import steps as climb_steps
from tests.test_scenario_router import _FakeChannel, _case_step


def _cases() -> list[TestCaseListItem]:
    return [
        TestCaseListItem(
            id=case_id, scene="Shop", step=f"상점 확인 {case_id}", expected_value="보인다",
            verification_status="VERIFIED",
        )
        for case_id in (1, 2, 3)
    ]


def _free_current(scenario_id: int = 9, case_ids: tuple[int, ...] = (1, 2)) -> ScenarioPlan:
    # 저장된 FREE 스텝은 `step_source` 가 비어 올 수 있다 — orchestration 이 저장한 그대로다.
    return ScenarioPlan(
        scenario_id=scenario_id, title="상점 묶음", description="상점 TC 묶음",
        steps=[AuthoredStep(action="", case_id=case_id) for case_id in case_ids],
        execution_mode="FREE",
    )


def _authoring(monkeypatch, plans, writers=None):
    import app.agents.scenario.workflow as wf

    calls = {"writer": 0, "scored": []}
    grouping_plans = list(plans)
    writer_answers = list(writers or [])

    async def fake_call(model, reasoning, schema, prompt):
        if schema is wf.GroupingPlan:
            return grouping_plans.pop(0)
        calls["writer"] += 1
        return writer_answers.pop(0)

    async def fake_score(request, groups):
        calls["scored"].append([group.title for group in groups])
        return []

    monkeypatch.setattr(wf, "_call", fake_call)
    monkeypatch.setattr(wf, "_score_groups", fake_score)
    return wf, calls


def test_current_scenarios_without_a_mode_read_as_ordered() -> None:
    request = ScenarioAgentRequest.model_validate({
        "user_input": "고쳐줘",
        "current_scenarios": [
            {"scenario_id": "4", "title": "옛 시나리오", "description": "", "steps": []},
            {"scenario_id": 5, "title": "묶음", "description": "", "steps": [],
             "execution_mode": "FREE"},
        ],
    })

    old, free = request.current_scenarios
    assert old.execution_mode is None and old.mode() == "ORDERED" and not old.is_free()
    assert free.mode() == "FREE" and free.is_free()


def test_free_steps_are_one_per_unique_case_in_order() -> None:
    made = free_steps([3, 1, 3, 2])

    assert [(s.action, s.case_id, s.step_source) for s in made] == [
        ("", 3, "CASE"), ("", 1, "CASE"), ("", 2, "CASE"),
    ]


def test_a_free_group_skips_the_writer_and_submits_free_steps(monkeypatch) -> None:
    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="상점 묶음", case_ids=[2, 1, 2, 99], execution_mode="FREE"),
        wf_mod.Group(title="상점 여정", case_ids=[3]),
    ])
    wf, calls = _authoring(
        monkeypatch, [plan], writers=[wf_mod._Writer(steps=[_case_step(3)])]
    )
    channel = _FakeChannel()

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="상점 TC 순서 없이 묶어줘", test_case_list=_cases()), _CTX, channel,
    ))

    free, ordered = channel.submitted
    assert free["execution_mode"] == "FREE"
    # 모르는 번호 99 는 빠지고, 중복은 한 번만, 묶은 순서대로.
    assert free["steps"] == [
        {"action": "", "case_id": 2, "hint": None, "input": None, "step_source": "CASE",
         "step_source_capability_id": None, "step_unknown_reason": None},
        {"action": "", "case_id": 1, "hint": None, "input": None, "step_source": "CASE",
         "step_source_capability_id": None, "step_unknown_reason": None},
    ]
    assert free["description"]  # writer 가 없어도 설명은 빈칸으로 나가지 않는다
    assert ordered["execution_mode"] == "ORDERED"
    assert calls["writer"] == 1                      # ORDERED 묶음만 writer 를 부른다
    assert calls["scored"] == [["상점 여정"]]         # 걷기 검증도 ORDERED 묶음만
    assert out.reviewed is not None and out.reviewed.included == [1, 2, 3]


def test_only_free_groups_call_neither_writer_nor_scoring(monkeypatch) -> None:
    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="묶음", case_ids=[1, 2], execution_mode="FREE", description="상점 TC 둘"),
    ])
    wf, calls = _authoring(monkeypatch, [plan])
    channel = _FakeChannel()

    asyncio.run(wf.run_authoring_workflow(
        _request(user_input="TC 묶음으로", test_case_list=_cases()), _CTX, channel,
    ))

    assert calls["writer"] == 0 and calls["scored"] == []
    assert channel.submitted[0]["description"] == "상점 TC 둘"


def test_a_group_reusing_a_free_scenario_stays_free(monkeypatch) -> None:
    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="상점 묶음", case_ids=[1, 2, 3], scenario_id=9),
    ])
    wf, calls = _authoring(monkeypatch, [plan])
    channel = _FakeChannel()

    asyncio.run(wf.run_authoring_workflow(
        _request(user_input="같은 범위로 다시", test_case_list=_cases(),
                 current_scenarios=[_free_current()]),
        _CTX, channel,
    ))

    assert channel.submitted[0]["scenario_id"] == 9
    assert channel.submitted[0]["execution_mode"] == "FREE"
    assert calls["writer"] == 0


def test_a_declined_free_group_is_dropped_without_a_rewrite(monkeypatch) -> None:
    plan = wf_mod.GroupingPlan(groups=[
        wf_mod.Group(title="묶음", case_ids=[1], execution_mode="FREE"),
    ])
    wf, calls = _authoring(monkeypatch, [plan])
    channel = _FakeChannel(answers=[ScenarioAccepted(accepted=False, detail="invalid_free_scenario")])

    out = asyncio.run(wf.run_authoring_workflow(
        _request(user_input="순서 없이", test_case_list=_cases()), _CTX, channel,
    ))

    assert len(channel.submitted) == 1 and calls["writer"] == 0
    assert out.reviewed is not None and out.reviewed.included == []


def _modify(monkeypatch, plans):
    import app.agents.scenario.workflow as wf

    prompts: list[str] = []

    async def fake_call(model, reasoning, schema, prompt):
        prompts.append("\n".join(prompt))
        return plans.pop(0)

    monkeypatch.setattr(wf, "_call", fake_call)
    return wf, prompts


def test_modify_keeps_a_free_scenario_free(monkeypatch) -> None:
    wf, prompts = _modify(monkeypatch, [wf_mod.ModifyPlan(scenario_id=9, case_ids=[1, 2, 3])])
    channel = _FakeChannel()

    asyncio.run(wf.run_modify_workflow(
        _request(user_input="3번 TC 도 넣어줘", test_case_list=_cases(),
                 current_scenarios=[_free_current()]),
        _CTX, channel,
    ))

    assert "[FREE" in prompts[0]  # 모델이 대상이 FREE 인 것을 읽는다
    submitted = channel.submitted[0]
    assert submitted["scenario_id"] == 9 and submitted["execution_mode"] == "FREE"
    assert [(s["action"], s["case_id"]) for s in submitted["steps"]] == [("", 1), ("", 2), ("", 3)]


def test_modify_does_not_save_a_free_scenario_with_the_same_cases(monkeypatch) -> None:
    same = wf_mod.ModifyPlan(scenario_id=9, case_ids=[2, 1])
    wf, prompts = _modify(monkeypatch, [same, same.model_copy()])
    channel = _FakeChannel()

    asyncio.run(wf.run_modify_workflow(
        _request(user_input="고쳐줘", test_case_list=_cases(), current_scenarios=[_free_current()]),
        _CTX, channel,
    ))

    assert len(prompts) == 2 and channel.submitted == []


def test_modify_converts_an_ordered_scenario_to_free_when_asked(monkeypatch) -> None:
    ordered = ScenarioPlan(
        scenario_id=7, title="상점 여정", description="상점", steps=[_case_step(1), _case_step(2)],
    )
    wf, _ = _modify(monkeypatch, [
        wf_mod.ModifyPlan(scenario_id=7, execution_mode="FREE", case_ids=[1, 2]),
    ])
    channel = _FakeChannel()

    asyncio.run(wf.run_modify_workflow(
        _request(user_input="이거 순서 없이 바꿔줘", test_case_list=_cases(),
                 current_scenarios=[ordered]),
        _CTX, channel,
    ))

    submitted = channel.submitted[0]
    assert submitted["execution_mode"] == "FREE"
    assert [s["case_id"] for s in submitted["steps"]] == [1, 2]
    assert all(s["action"] == "" for s in submitted["steps"])


def test_modify_of_an_ordered_scenario_stays_ordered(monkeypatch) -> None:
    ordered = ScenarioPlan(
        scenario_id=7, title="상점 여정", description="상점", steps=[_case_step(1)],
    )
    wf, _ = _modify(monkeypatch, [
        wf_mod.ModifyPlan(scenario_id=7, steps=[_case_step(1), _case_step(2)]),
    ])
    channel = _FakeChannel()

    asyncio.run(wf.run_modify_workflow(
        _request(user_input="2번도 넣어줘", test_case_list=_cases(), current_scenarios=[ordered]),
        _CTX, channel,
    ))

    submitted = channel.submitted[0]
    assert submitted["execution_mode"] == "ORDERED"
    assert submitted["steps"][0]["action"] == "상점을 열어 확인한다"


class _SubmitChannel:
    def __init__(self) -> None:
        self.submitted: list[dict] = []

    async def submit_scenario(self, scenario: dict, absorbed_scenario_ids=None, proposed_cases=None):
        self.submitted.append(scenario)
        return ScenarioAccepted(accepted=True, written=len(self.submitted))


def _submit_tool(channel):
    tools = build_tools(channel, TestCaseSearchState(), has_test_case_list=True)
    return next(t for t in tools if t.name == "submit_scenario")


def test_the_submit_tool_carries_the_execution_mode() -> None:
    channel = _SubmitChannel()
    submit = _submit_tool(channel)

    asyncio.run(submit.ainvoke({
        "title": "묶음", "description": "d",
        "steps": [{"action": "", "case_id": 1, "step_source": "CASE"}],
        "execution_mode": "FREE",
    }))
    asyncio.run(submit.ainvoke({
        "title": "여정", "description": "d", "steps": [{"action": "연다", "case_id": 1}],
    }))

    assert channel.submitted[0]["execution_mode"] == "FREE"
    assert channel.submitted[1]["execution_mode"] is None  # 말하지 않으면 orchestration 이 정한다


@pytest.mark.parametrize(("mode", "agents_built"), [("FREE", 1), (None, 2)])
def test_settle_order_leaves_a_free_scenario_alone(mode, agents_built) -> None:
    """FREE 는 순서가 없으니 오름 검사로 다시 쓰지 않는다. 같은 스텝의 ORDERED 는 다시 쓴다."""
    climbing = climb_steps((1636, ""), (1637, ""), (1639, ""))
    result = ScenarioAgentResult(message="ok", scenarios=[
        ScenarioPlan(title="지도 묶음", description="", steps=climbing, execution_mode=mode),
    ])
    built: list[str] = []

    def factory(*, model, tools, system_prompt, reasoning=None):
        built.append(system_prompt)
        return RunnableLambda(lambda _inputs: {"messages": [], "structured_response": result})

    agent = ScenarioAgent(agent_factory=factory)
    out = asyncio.run(agent.run(_request(test_case_list=CLIMB_CASES), _CTX, _channel()))

    assert len(built) == agents_built  # 2 = 첫 루프 + 다시 쓰기 agent
    assert out.scenarios[0].execution_mode == mode
