# -*- coding: utf-8 -*-
"""저작 답의 세 칸 — 결과 · 설명 · 질문 (ARTEL-927).

한 덩어리 글이던 답을 나눈다. 결과는 코드가 센 사실만 짧게, 설명은 판단한 모델이,
질문은 사용자가 고를 수 있는 모양(선택지)으로. `message` 는 세 칸을 이은 글로 남는다 —
Redis 대화 기록과 구형 화면이 그것을 읽는다.
"""

import asyncio as aio

from app.sessions.channel import ScenarioAccepted
from tests.test_agents_scenario import _CTX, _request
from tests.test_scenario_router import (
    _FakeChannel,
    _case_list,
    _case_step,
    _current_scenario,
    _modify_agent,
    _workflow_agent,
)


def _author(monkeypatch, plan, writers, channel=None):
    wf, calls = _workflow_agent(monkeypatch, plan, writers)
    channel = channel or _FakeChannel()
    out = aio.run(wf.run_authoring_workflow(
        _request(user_input="짜줘", test_case_list=_case_list()), _CTX, channel,
    ))
    return out, channel, calls


# --- 저작 ------------------------------------------------------------------------


def test_authoring_reply_splits_result_from_detail(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(
        groups=[wf.Group(title="상점 열기", case_ids=[1])],
        detail="상점으로 가는 길이 하나라 그 길로만 묶었어요.",
    )
    out, _, _ = _author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])])

    assert out.reply is not None
    # 결과는 코드가 센 사실 — 무엇을 몇 스텝으로 저장했는지. 이름은 따옴표가 아니라 강조.
    assert "**상점 열기**" in out.reply.result and "1스텝" in out.reply.result
    assert "'" not in out.reply.result
    # 설명은 판단한 쪽(모델)의 말 그대로.
    assert out.reply.detail == "상점으로 가는 길이 하나라 그 길로만 묶었어요."
    # 대화 기록용 글은 두 칸을 다 담는다.
    assert out.reply.result in out.message and out.reply.detail in out.message
    assert out.questions == []


def test_authoring_reply_counts_several_scenarios(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(groups=[
        wf.Group(title="첫 여정", case_ids=[1]),
        wf.Group(title="둘째 여정", case_ids=[1]),
    ])
    writers = [wf._Writer(steps=[_case_step()]), wf._Writer(steps=[_case_step()])]
    out, _, _ = _author(monkeypatch, plan, writers)

    assert "2개" in out.reply.result
    assert "**첫 여정**" in out.reply.result and "**둘째 여정**" in out.reply.result
    assert "2스텝" in out.reply.result  # 저쪽이 센 스텝 합


def test_model_questions_become_answerable_questions(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(
        groups=[wf.Group(title="상점 열기", case_ids=[1])],
        questions=[
            wf.Ask(text="구매 실패도 넣을까요?", why="골드가 모자랄 때 막히는지 따로 봐야 해요.",
                   options=["구매 실패도 넣어 줘", "지금처럼 둬"]),
            wf.Ask(text="상점 닫기도 볼까요?", options=["닫기도 넣어 줘", "필요 없어"]),
        ],
    )
    out, channel, _ = _author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])])

    assert len(channel.submitted) == 1  # 물으면서도 할 수 있는 것은 저장한다
    assert [q.text for q in out.questions] == ["구매 실패도 넣을까요?", "상점 닫기도 볼까요?"]
    first = out.questions[0]
    assert first.why == "골드가 모자랄 때 막히는지 따로 봐야 해요."
    assert [o.label for o in first.options] == ["구매 실패도 넣어 줘", "지금처럼 둬"]
    # 답이 어느 질문의 것인지 이으려면 번호가 서로 달라야 한다.
    ids = [q.id for q in out.questions] + [o.id for q in out.questions for o in q.options]
    assert all(ids) and len(set(q.id for q in out.questions)) == 2
    assert len({o.id for o in first.options}) == 2
    # 대화 기록에도 물은 것이 남는다 — 다음 턴이 "그거"를 풀 수 있어야 한다.
    assert "구매 실패도 넣을까요?" in out.message


def test_a_question_only_turn_says_nothing_was_saved(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(groups=[], questions=[wf.Ask(text="엔딩 전이 보스 포함인가요?")])
    out, channel, calls = _author(monkeypatch, plan, [])

    assert channel.submitted == [] and not calls["c"]
    assert out.reply is not None and "저장하지 않았" in out.reply.result
    assert [q.text for q in out.questions] == ["엔딩 전이 보스 포함인가요?"]
    assert "엔딩 전이 보스 포함인가요?" in out.message


def test_an_unplaced_case_becomes_a_question_with_choices(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(groups=[wf.Group(title="여정", case_ids=[1])])
    writers = [wf._Writer(steps=[]), wf._Writer(steps=[])]  # 보수까지 빈손
    out, _, _ = _author(monkeypatch, plan, writers)

    assert len(out.questions) == 1
    asked = out.questions[0]
    assert "Shop — 상점을 연다" in asked.text  # 번호가 아니라 이름으로
    assert "1" not in asked.text.replace("Shop — 상점을 연다", "")
    assert len(asked.options) == 2


def test_a_dropped_scenario_becomes_a_question(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(groups=[wf.Group(title="여정", case_ids=[1])])
    writers = [wf._Writer(steps=[_case_step()]), wf._Writer(steps=[_case_step()])]
    channel = _FakeChannel(answers=[
        ScenarioAccepted(accepted=False, detail="근거가 어긋납니다"),
        ScenarioAccepted(accepted=False, detail="같은 이유"),
    ])
    out, _, _ = _author(monkeypatch, plan, writers, channel)

    assert "저장한 시나리오가 없" in out.reply.result
    assert any("**여정**" in q.text for q in out.questions)
    assert all("검수" not in q.text for q in out.questions)


# --- 수정 ------------------------------------------------------------------------


def _modify(monkeypatch, plans, channel=None, current=None):
    wf, calls = _modify_agent(monkeypatch, plans)
    channel = channel or _FakeChannel()
    out = aio.run(wf.run_modify_workflow(
        _request(
            user_input="상점 여정 고쳐줘", test_case_list=_case_list(),
            current_scenarios=current or [_current_scenario()],
        ),
        _CTX, channel,
    ))
    return out, channel


def test_modify_reply_splits_result_from_detail(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plans = [wf.ModifyPlan(
        scenario_id=7, steps=[_case_step(), _case_step()], detail="3번 스텝을 나눴어요.",
    )]
    out, _ = _modify(monkeypatch, plans)

    assert "**상점 여정**" in out.reply.result and "2스텝" in out.reply.result
    assert "'" not in out.reply.result
    assert out.reply.detail == "3번 스텝을 나눴어요."


def test_an_unclear_target_asks_with_the_titles_as_choices(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plans = [wf.ModifyPlan()]  # 대상도 질문도 없음 — 코드가 묻는다
    current = [_current_scenario(7, "상점 여정"), _current_scenario(8, "전투 여정")]
    out, channel = _modify(monkeypatch, plans, current=current)

    assert channel.submitted == []
    assert out.reply is not None and "그대로" in out.reply.result
    assert len(out.questions) == 1
    labels = [o.label for o in out.questions[0].options]
    assert any("상점 여정" in label for label in labels)
    assert any("전투 여정" in label for label in labels)


def test_a_modify_question_is_relayed_as_a_question(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plans = [wf.ModifyPlan(questions=[
        wf.Ask(text="따로 시나리오로 만들까요?", options=["따로 만들어 줘", "그냥 둬"]),
    ])]
    out, channel = _modify(monkeypatch, plans)

    assert channel.submitted == []
    assert [q.text for q in out.questions] == ["따로 시나리오로 만들까요?"]


# --- 결과 프레임 ----------------------------------------------------------------


def test_the_result_frame_carries_reply_and_questions() -> None:
    from app.agents.scenario.schemas import (
        AgentReply, QuestionOption, ScenarioAgentResult, ScenarioQuestion,
    )
    from app.api.sessions import _result_event

    result = ScenarioAgentResult(
        message="저장했어요\n이유",
        reply=AgentReply(result="저장했어요", detail="이유"),
        questions=[ScenarioQuestion(
            id="agent:1", text="넣을까요?",
            options=[QuestionOption(id="o1", label="넣어 줘")],
        )],
    )
    event = _result_event(result)

    assert event["reply"] == {"result": "저장했어요", "detail": "이유"}
    assert event["questions"][0]["id"] == "agent:1"
    assert event["questions"][0]["options"][0]["label"] == "넣어 줘"


def test_a_plain_reply_leaves_both_keys_out() -> None:
    """인사·잡담·실패 문구는 글만 — 키를 아예 빼야 저쪽이 예전처럼 읽는다."""
    from app.agents.scenario.schemas import ScenarioAgentResult
    from app.api.sessions import _result_event

    event = _result_event(ScenarioAgentResult(message="안녕하세요"))

    assert "reply" not in event and "questions" not in event
