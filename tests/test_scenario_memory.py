# -*- coding: utf-8 -*-
"""저작 Agent 의 대화 기억 (ARTEL-942).

B·E 는 tail 에서 이번 요청 앞에 과거 대화를 읽는다. prefix(규칙·게임 모양·TC 전량)는
대화가 있든 없든 바이트까지 같아야 한다 — 캐시가 프로젝트 단위로 나눠 쓰이는 것이 그 덕이다
(POC: 다른 세션이 쓴 캐시를 읽음). 기억은 대화 수와 글자 수 둘 다로 묶는다.
"""

from langchain_core.messages import AIMessage, HumanMessage

from tests.test_agents_scenario import _request
from tests.test_scenario_router import _case_list, _current_scenario


def _with_history(history, **kwargs):
    return _request(
        user_input="이제 전투도 짜줘", test_case_list=_case_list(),
        current_scenarios=[_current_scenario()], history=history, **kwargs,
    )


def test_no_history_means_no_section() -> None:
    import app.agents.scenario.workflow as wf

    assert wf._conversation(_with_history([])) == ""


def test_the_conversation_reads_oldest_first_with_who_said_it() -> None:
    import app.agents.scenario.workflow as wf

    text = wf._conversation(_with_history([
        HumanMessage("참고로 스토리가 끝나면 맵으로 이동해"),
        AIMessage("알겠어요."),
    ]))

    assert "CONVERSATION SO FAR" in text
    assert text.index("사용자: 참고로 스토리가 끝나면 맵으로 이동해") < text.index("Artel: 알겠어요.")
    assert "cut off" not in text


def test_one_long_message_is_cut_and_says_so(monkeypatch) -> None:
    """한 번에 아주 긴 입력 — 그 턴에는 전문이 쓰였고, 기억에는 앞부분만 남긴다."""
    import app.agents.scenario.workflow as wf

    monkeypatch.setattr(wf, "_history_limits", lambda: (10_000, 50))
    text = wf._conversation(_with_history([HumanMessage("가" * 500), AIMessage("네")]))

    assert "가" * 50 in text and "가" * 51 not in text
    assert "생략" in text


def test_the_oldest_turns_drop_first_when_over_the_budget(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf

    monkeypatch.setattr(wf, "_history_limits", lambda: (40, 1_000))
    text = wf._conversation(_with_history([
        HumanMessage("오래된 말 " * 5), AIMessage("오래된 답 " * 5),
        HumanMessage("최근 말"), AIMessage("최근 답"),
    ]))

    assert "최근 말" in text and "최근 답" in text
    assert "오래된" not in text
    # 잘렸다는 것을 모델이 안다 — "원래 없던 말" 과 "밀려난 말" 을 가르게.
    assert "cut off" in text


def test_a_window_already_trimmed_upstream_is_marked() -> None:
    import app.agents.scenario.workflow as wf

    text = wf._conversation(_with_history([HumanMessage("최근 말")], history_truncated=True))

    assert "cut off" in text


def test_grouping_reads_the_conversation_before_the_request_and_keeps_the_prefix() -> None:
    import app.agents.scenario.workflow as wf

    history = [HumanMessage("참고로 스토리가 끝나면 맵으로 이동해"), AIMessage("알겠어요.")]
    prefix_without, tail_without = wf._grouping_prompt(_with_history([]))
    prefix_with, tail_with = wf._grouping_prompt(_with_history(history))

    assert prefix_with == prefix_without  # 캐시가 깨지지 않는다
    assert "CONVERSATION SO FAR" not in tail_without
    assert tail_with.index("CONVERSATION SO FAR") < tail_with.index("=== USER REQUEST ===")
    assert tail_with.endswith(tail_without)  # 대화는 앞에 붙기만 한다


def test_modify_reads_the_conversation_before_the_request_and_keeps_the_prefix() -> None:
    import app.agents.scenario.workflow as wf

    history = [HumanMessage("맵까지 짜줘"), AIMessage("맵 도착 뒤 확인은 범위 밖이라 넣지 않았어요.")]
    prefix_without, tail_without = wf._modify_prompt(_with_history([]))
    prefix_with, tail_with = wf._modify_prompt(_with_history(history))

    assert prefix_with == prefix_without
    assert "맵 도착 뒤 확인은 범위 밖이라 넣지 않았어요." in tail_with
    assert tail_with.endswith(tail_without)


def test_the_writer_does_not_get_the_conversation() -> None:
    """C 는 B 가 고른 케이스로 문장만 쓴다. 갈래 수만큼 곱해지는 호출이라 싣지 않는다."""
    import app.agents.scenario.workflow as wf

    history = [HumanMessage("참고로 스토리가 끝나면 맵으로 이동해")]
    group = wf.Group(title="여정", case_ids=[1])
    _, tail = wf._writer_prompt(_with_history(history), group)

    assert "CONVERSATION SO FAR" not in tail


# --- 라우터 (ARTEL-942, POC 3) ---------------------------------------------------
#
# "그거 말고", "응 그렇게 해줘" 는 직전 Agent 답을 모르면 갈래를 정할 수 없다. 120자에서 자르던
# 때는 답 끝의 질문·뺀 것이 잘려 9/12, 직전 턴을 자르지 않으니 12/12, 창 전체는 8/12 였다.


def test_the_router_sees_the_whole_last_turn() -> None:
    from app.agents.scenario.agent import _hint

    reply = "총 1건의 시나리오를 생성했습니다.\n" + "설명 " * 80 + "\n맵 도착 뒤 화면 확인도 넣을까요?"
    hint = _hint(_with_history([HumanMessage("오래된 말"), AIMessage("오래된 답"),
                                HumanMessage("맵까지 짜줘"), AIMessage(reply)]))

    assert "맵 도착 뒤 화면 확인도 넣을까요?" in hint  # 답 끝의 질문이 잘리지 않는다
    assert "맵까지 짜줘" in hint
    assert "오래된" not in hint  # 창 전체는 주지 않는다 — 직전 답이 묻힌다


def test_the_router_still_caps_one_huge_message(monkeypatch) -> None:
    import app.agents.scenario.workflow as wf
    from app.agents.scenario.agent import _hint

    monkeypatch.setattr(wf, "_history_limits", lambda: (10_000, 30))
    hint = _hint(_with_history([HumanMessage("가" * 500), AIMessage("네")]))

    assert "가" * 31 not in hint
