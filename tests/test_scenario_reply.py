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
    # 결과는 코드가 센 사실 — 몇 건을 새로 만들고 고쳤는지(ARTEL-936). 스텝 수는 없다.
    assert out.reply.result == "총 1건의 시나리오를 생성했습니다."
    assert [(c.action, c.title) for c in out.reply.changes] == [("created", "상점 열기")]
    # 설명은 판단한 쪽(모델)의 말 그대로.
    assert out.reply.detail == "상점으로 가는 길이 하나라 그 길로만 묶었어요."
    # 대화 기록용 글은 두 칸을 다 담는다.
    assert out.reply.result in out.message and out.reply.detail in out.message
    assert "- 상점 열기" in out.message  # 대화 기록에도 무엇이 바뀌었는지 남는다
    assert out.questions == []


def test_authoring_reply_counts_several_scenarios(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(groups=[
        wf.Group(title="첫 여정", case_ids=[1]),
        wf.Group(title="둘째 여정", case_ids=[1]),
    ])
    writers = [wf._Writer(steps=[_case_step()]), wf._Writer(steps=[_case_step()])]
    out, _, _ = _author(monkeypatch, plan, writers)

    assert out.reply.result == "총 2건의 시나리오를 생성했습니다."
    assert [c.title for c in out.reply.changes] == ["첫 여정", "둘째 여정"]
    assert "스텝" not in out.reply.result


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
    assert "[[tc:1]]" in asked.text  # 맨 번호가 아니라 표식 — 화면이 이름 칩으로 그린다
    assert "1" not in asked.text.replace("[[tc:1]]", "")
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

    assert out.reply.result == "총 1건의 시나리오를 수정했습니다."
    assert [(c.action, c.title, c.scenario_id) for c in out.reply.changes] == [("updated", "상점 여정", 7)]
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

    assert event["reply"] == {"result": "저장했어요", "detail": "이유", "changes": []}
    assert event["questions"][0]["id"] == "agent:1"
    assert event["questions"][0]["options"][0]["label"] == "넣어 줘"


def test_a_plain_reply_leaves_both_keys_out() -> None:
    """인사·잡담·실패 문구는 글만 — 키를 아예 빼야 저쪽이 예전처럼 읽는다."""
    from app.agents.scenario.schemas import ScenarioAgentResult
    from app.api.sessions import _result_event

    event = _result_event(ScenarioAgentResult(message="안녕하세요"))

    assert "reply" not in event and "questions" not in event


# --- 말투 (ARTEL-930) -------------------------------------------------------------
#
# 사용자의 화면에 없는 이 도구의 말 — 대화창에 나가면 사용자는 무엇을 가리키는지 모른다.
_INTERNAL_WORDS = ("갈래", "여정", "묶음", "검수", "판정", "저작", "케이스")


def _no_internal_words(text: str) -> None:
    found = [word for word in _INTERNAL_WORDS if word in text]
    assert not found, f"내부 용어 {found}: {text}"


def test_canned_replies_use_the_words_on_screen() -> None:
    from app.agents.scenario.agent import _CANNED

    for reply in _CANNED.values():
        _no_internal_words(reply)


def test_workflow_lines_use_the_words_on_screen(monkeypatch) -> None:
    """코드가 쓰는 결과 줄과 질문 — 저장·미저장·자리 못 찾음·대상 불분명을 한 번씩 지난다."""
    import app.agents.scenario.workflow as wf

    said: list[str] = []

    def collect(out) -> None:
        said.append(out.message)
        said.extend(o.label for q in out.questions for o in q.options)

    plan = wf.GroupingPlan(groups=[wf.Group(title="상점 열기", case_ids=[1])])
    collect(_author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])])[0])
    plan = wf.GroupingPlan(groups=[wf.Group(title="상점 열기", case_ids=[1])])
    collect(_author(monkeypatch, plan, [wf._Writer(steps=[]), wf._Writer(steps=[])])[0])
    plan = wf.GroupingPlan(groups=[])
    collect(_author(monkeypatch, plan, [])[0])
    collect(_modify(monkeypatch, [wf.ModifyPlan()])[0])
    collect(_modify(monkeypatch, [wf.ModifyPlan(scenario_id=7, steps=[_case_step(), _case_step()])])[0])

    for text in said:
        # 시나리오 제목은 사용자가 지은 말이라 빼고 본다.
        _no_internal_words(text.replace("상점 여정", "").replace("상점 열기", ""))


# --- TC·TS 참조 (ARTEL-931) ------------------------------------------------------
#
# 모델은 TC·TS 를 `[[tc:N]]`·`[[ts:N]]` 표식으로만 가리킨다. 번호는 표식 안에서 기계만
# 읽고, 화면은 refs 의 이름으로 칩을 그린다. 대화 기록(`message`)에는 이름 글자로 남는다.


def test_a_known_tc_marker_is_kept_and_named(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(
        groups=[wf.Group(title="상점 열기", case_ids=[1])],
        detail="[[tc:1]] 하나로 충분해서 그것만 넣었어요.",
    )
    out, _, _ = _author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])])

    assert "[[tc:1]]" in out.reply.detail
    assert len(out.refs) == 1
    ref = out.refs[0]
    assert (ref.kind, ref.id, ref.label) == ("tc", 1, "Shop — 상점을 연다")
    assert "상점이 열린다" in (ref.detail or "")  # 기대값이 카드에 실린다
    # 대화 기록은 사람이 읽는 글 — 표식 대신 이름이다.
    assert "@TC Shop — 상점을 연다" in out.message and "[[" not in out.message


def test_an_unknown_marker_is_dropped(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(
        groups=[wf.Group(title="상점 열기", case_ids=[1])],
        detail="[[tc:999]] 는 빼고 [[ts:42]] 와 겹치지 않게 했어요.",
    )
    out, _, _ = _author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])])

    # 이 프로젝트에 없는 번호는 지운다 — 지어낸 번호가 칩이 되면 누를 곳이 없다.
    assert "999" not in out.reply.detail and "42" not in out.reply.detail
    assert out.refs == []


def test_a_ts_marker_names_the_current_scenario(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plans = [wf.ModifyPlan(
        scenario_id=7, steps=[_case_step(), _case_step()],
        detail="[[ts:7]] 마지막에 한 스텝을 더했어요.",
    )]
    out, _ = _modify(monkeypatch, plans)

    assert [(r.kind, r.id, r.label) for r in out.refs] == [("ts", 7, "상점 여정")]
    assert "@TS 상점 여정" in out.message


def test_the_unplaced_question_points_with_a_marker(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(groups=[wf.Group(title="여정", case_ids=[1])])
    out, _, _ = _author(monkeypatch, plan, [wf._Writer(steps=[]), wf._Writer(steps=[])])

    asked = out.questions[0]
    assert "[[tc:1]]" in asked.text
    assert ("tc", 1) in {(r.kind, r.id) for r in out.refs}


def test_options_never_carry_a_marker(monkeypatch) -> None:
    """보기는 누르면 사용자의 말로 돌아온다 — 표식이 그대로 말풍선에 찍히면 안 된다."""
    import app.agents.scenario.workflow as wf

    plan = wf.GroupingPlan(
        groups=[wf.Group(title="상점 열기", case_ids=[1])],
        questions=[wf.Ask(text="[[tc:1]] 도 따로 볼까요?", options=["[[tc:1]] 따로 만들어 줘", "그냥 둬"])],
    )
    out, _, _ = _author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])])

    labels = [o.label for o in out.questions[0].options]
    assert labels[0] == "Shop — 상점을 연다 따로 만들어 줘"
    assert all("[[" not in label for label in labels)


def test_the_result_frame_carries_refs() -> None:
    from app.agents.scenario.schemas import AgentReply, Ref, ScenarioAgentResult
    from app.api.sessions import _result_event

    event = _result_event(ScenarioAgentResult(
        message="m", reply=AgentReply(result="r"),
        refs=[Ref(kind="tc", id=1, label="Shop — 상점을 연다", detail="상점이 열린다")],
    ))

    assert event["refs"] == [{"kind": "tc", "id": 1, "label": "Shop — 상점을 연다", "detail": "상점이 열린다"}]


# --- 바뀐 시나리오 목록 (ARTEL-936) ---------------------------------------------------


def test_changes_follow_what_orche_actually_saved(monkeypatch) -> None:
    """무엇을 새로 만들고 고쳤는지는 저장한 쪽이 센다. 하나를 냈는데 둘로 나뉘어 저장되거나,
    같은 제목이라 기존 것을 고쳤으면 그대로 따른다."""
    import app.agents.scenario.workflow as wf
    from app.sessions.channel import SavedScenario

    plan = wf.GroupingPlan(groups=[wf.Group(title="상점 열기", case_ids=[1])])
    channel = _FakeChannel(answers=[ScenarioAccepted(
        accepted=True, written=1, steps=3,
        saved=[
            SavedScenario(scenario_id=40, title="상점 열기", created=False),
            SavedScenario(scenario_id=41, title="상점 열기 (2)", created=True),
        ],
    )])
    out, _, _ = _author(monkeypatch, plan, [wf._Writer(steps=[_case_step()])], channel)

    assert [(c.action, c.title, c.scenario_id) for c in out.reply.changes] == [
        ("updated", "상점 열기", 40), ("created", "상점 열기 (2)", 41),
    ]
    assert out.reply.result == "총 1건의 시나리오를 생성하고 1건을 수정했습니다."


def test_a_merge_lists_the_absorbed_ones_as_removed(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    plans = [wf.ModifyPlan(scenario_id=7, steps=[_case_step(), _case_step()], absorbed_scenario_ids=[8])]
    channel = _FakeChannel(answers=[ScenarioAccepted(accepted=True, steps=2, absorbed=["`전투` 여정"])])
    current = [_current_scenario(7, "상점 여정"), _current_scenario(8, "`전투` 여정")]
    out, _ = _modify(monkeypatch, plans, channel=channel, current=current)

    assert [(c.action, c.title) for c in out.reply.changes] == [("updated", "상점 여정"), ("removed", "전투 여정")]
    assert out.reply.result == "총 1건의 시나리오를 수정하고 1건을 삭제했습니다."


def test_titles_lose_backticks() -> None:
    """제목에 섞여 오는 `Canvas/continue` 같은 코드 표시는 목록에서 글자로만 보인다."""
    import app.agents.scenario.workflow as wf

    assert wf._plain_title("게임 시작 후 `Canvas/continue`로 진행") == "게임 시작 후 Canvas/continue로 진행"
