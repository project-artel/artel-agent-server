"""등록된 macro 가 런을 넘어 사는 길 (ARTEL-921).

`register_macro` 가 `MACRO_REGISTER` 로 `content_map` 에 적고, `read_macro` 와
`run_macro` 가 이 런이 모르는 이름을 `MACRO_READ` 로 묻는다.

넷을 못박는다. 넷은 서로 다른 방식으로 깨진다:

- **프레임이 계약대로 나가는가** — 타입 문자열과 필드 철자는 orchestration 의
  `contentmap/macro/MacroWriteFrames.kt` 가 정한 것이다. PR 135 는 없는 frame 을 이
  저장소가 지어냈다가 통째로 닫혔고, 이 검사가 그것을 되풀이하지 않게 하는 자리다.
- **쓰기 실패가 런을 안 죽이는가** — 저쪽이 거절해도, 답이 안 와도, 보내다 터져도
  tool 은 문장을 돌려주고 그 macro 는 이번 런 안에서 계속 부를 수 있다.
- **답 없음을 실패로 옮기지 않는가** — 이 프레임을 모르는 구버전 orchestration 은
  라우터에서 프레임을 떨어뜨리고 거절이 안 돌아온다. 그때 "안 됐다" 고 하면 모델이 같은
  정의를 계속 다시 보낸다.
- **지난 런이 등록한 것을 이번 런이 부르는가** — `read_macro` 와 `run_macro` 가 같은
  경로로 저쪽에서 가져온다.
"""

import asyncio

# `app.agents.qa.tools` 가 먼저 들어와야 한다. `app.qa` 를 먼저 들여오면
# `app.qa.schemas` 가 반쯤 초기화된 채로 `app.agents.qa.tools.state` 에 불려 순환
# import 로 터진다 — 다른 테스트 파일들도 같은 순서로 적혀 있다.
from tests.test_qa_macro_tools import (
    SIMPLE,
    actions,
    answer_macro,
    call,
    macro_frames,
    make,
    refuse_macro,
    settled,
    with_cards,
    with_reply,
)

from app.qa.envelope import MessageType

# 저쪽이 돌려주는 등록 답 하나. 필드 철자는 `MacroWriteFrames.kt` 의 것이고,
# id 는 전부 JSON 문자열이다.
STORED = {
    "type": "MACRO_REGISTER",
    "macro_id": "88",
    "name": "deal_a_card",
    "created": True,
    "screen_ids": ["12", "40"],
}

# 저쪽이 돌려주는 읽기 답 하나. `source` 가 이쪽이 실제로 쓰는 칸이다.
LOADED = {
    "type": "MACRO_READ",
    "macro_id": "88",
    "name": "deal_a_card",
    "source": SIMPLE,
    "definition": {"name": "deal_a_card"},
    "parameters": ["card"],
    "screen_ids": ["12"],
}


def drafted(screen_id: str = "12"):
    """초안 하나를 써 두고 어느 `screen` 에 서 있게 한다."""
    channel, state, tools, sent = make()
    with_cards(channel)
    settled(channel, screen_id=screen_id)
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)
    return channel, state, tools, sent


# --- MACRO_REGISTER 가 나간다 ---------------------------------------------------


def test_register_macro_sends_the_definition_to_the_content_map() -> None:
    """계약대로 나가는가. 철자가 저쪽의 `MacroRegisterRequest` 와 같아야 한다."""
    channel, _, tools, sent = drafted()

    with_reply(
        tools["register_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_REGISTER,
            MessageType.MACRO_WRITE_RESULT,
            STORED,
        ),
        name="deal_a_card",
        screens=["40"],
    )

    frames = macro_frames(sent, MessageType.MACRO_REGISTER)
    assert len(frames) == 1
    payload = frames[0]["payload"]
    assert payload["name"] == "deal_a_card"
    # 원본은 공백 하나까지 그대로다. 들여쓰기가 Python 문법의 일부고, 사람이 보는
    # diff 가 agent 가 적은 그대로여야 한다.
    assert payload["source"] == SIMPLE
    # 순서가 뜻을 가진다 — 저쪽이 실행 시점에 인자를 위치로 대응시킨다.
    assert payload["parameters"] == ["card"]
    # 서 있는 `screen` 이 먼저, agent 가 지목한 것이 뒤.
    assert payload["screens"] == ["12", "40"]


def test_the_definition_tree_carries_the_two_keys_the_far_side_reads() -> None:
    """저쪽의 `ck_macro_require_carries_remedy` 가 `kind` 와 `remedy` 에 기댄다.

    하나라도 이름이 바뀌면 `NOT jsonb_path_exists(...)` 가 어떤 tree 에도 참이 되어
    그 제약이 조용히 아무것도 막지 않는다. `tests/test_qa_macro_model.py` 가 모델 쪽에서
    같은 둘을 못박고, 이쪽은 **실제로 나가는 프레임** 안에서 본다.
    """
    channel, _, tools, sent = drafted()

    with_reply(
        tools["register_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_REGISTER,
            MessageType.MACRO_WRITE_RESULT,
            STORED,
        ),
        name="deal_a_card",
    )

    tree = macro_frames(sent, MessageType.MACRO_REGISTER)[0]["payload"]["definition"]
    requires = [
        statement
        for statement in tree["entry"]["statements"]
        if statement["kind"] == "require"
    ]
    assert len(requires) == 1
    assert requires[0]["remedy"] == "Draw a card first."


def test_a_stored_macro_is_reported_as_outliving_the_run() -> None:
    channel, _, tools, sent = drafted()

    answer = with_reply(
        tools["register_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_REGISTER,
            MessageType.MACRO_WRITE_RESULT,
            STORED,
        ),
        name="deal_a_card",
    )

    assert "later runs can call it" in answer
    # 관계를 더한 **뒤의** 상태다. 이번에 보낸 것만이 아니다.
    assert "12, 40" in answer


def test_an_update_in_place_is_not_reported_as_a_new_macro() -> None:
    """`created` 를 안 옮기면 모델이 방금 새 macro 를 만들었다고 읽는다."""
    channel, _, tools, sent = drafted()

    answer = with_reply(
        tools["register_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_REGISTER,
            MessageType.MACRO_WRITE_RESULT,
            {**STORED, "created": False},
        ),
        name="deal_a_card",
    )

    assert "updated in place" in answer


# --- 쓰기 실패가 런을 안 죽인다 --------------------------------------------------


def test_a_refused_write_leaves_the_macro_callable_in_this_run() -> None:
    """거절은 문장이고, 등록은 이 런의 책에 남는다.

    지도 쓰기 하나가 실패했다고 시나리오가 멈추면 이 tool 은 런이 지는 위험이지 보태는
    것이 아니다.
    """
    channel, state, tools, sent = drafted()

    answer = with_reply(
        tools["register_macro"],
        lambda: refuse_macro(
            channel,
            sent,
            MessageType.MACRO_REGISTER,
            "MACRO_REGISTER references screens outside this build's content map: [40]",
        ),
        name="deal_a_card",
    )

    assert "refused" in answer
    assert "this run only" in answer
    assert state.macros.registered("deal_a_card") is not None


def test_no_answer_is_not_reported_as_a_failed_write() -> None:
    """이 프레임을 모르는 구버전 orchestration 이 그대로 이 경로다.

    라우터가 프레임을 떨어뜨리고 거절이 안 돌아온다. 그때 "안 됐다" 고 하면 모델이 같은
    정의를 계속 다시 보낸다.
    """
    _, state, tools, sent = drafted()

    answer = call(tools["register_macro"], name="deal_a_card")

    assert "cannot be confirmed" in answer
    assert "Do not register it again" in answer
    assert "refused" not in answer and "could not be sent" not in answer
    # 프레임은 나갔고, 이 런 안에서는 부를 수 있다.
    assert len(macro_frames(sent, MessageType.MACRO_REGISTER)) == 1
    assert state.macros.registered("deal_a_card") is not None


def test_an_exception_on_the_way_out_does_not_end_the_run() -> None:
    channel, state, tools, sent = drafted()

    async def boom(payload):
        raise RuntimeError("the socket is gone")

    channel.register_macro = boom

    answer = call(tools["register_macro"], name="deal_a_card")

    assert "could not be sent" in answer
    assert "this run only" in answer
    assert state.macros.registered("deal_a_card") is not None


# --- 지난 런이 등록한 것을 이번 런이 읽는다 --------------------------------------


def test_read_macro_asks_the_content_map_for_a_name_this_run_does_not_know() -> None:
    channel, state, tools, sent = make()

    answer = with_reply(
        tools["read_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_READ,
            MessageType.MACRO_READ_RESULT,
            LOADED,
        ),
        name="deal_a_card",
    )

    assert macro_frames(sent, MessageType.MACRO_READ)[0]["payload"] == {
        "name": "deal_a_card"
    }
    assert SIMPLE in answer
    assert "earlier run" in answer
    # 이 런의 책에 들어왔으므로 `edit_macro` 와 `run_macro` 가 그것을 본다.
    registered = state.macros.registered("deal_a_card")
    assert registered is not None
    assert registered.screens == ("12",)
    assert state.macros.was_read("deal_a_card")


def test_read_macro_does_not_ask_twice_for_a_name_it_already_has() -> None:
    """초안이 있으면 저쪽에 안 묻는다. 왕복 하나를 그냥 버리는 일이다."""
    _, _, tools, sent = drafted()

    call(tools["read_macro"], name="deal_a_card")

    assert macro_frames(sent, MessageType.MACRO_READ) == []


def test_read_macro_says_what_this_run_knows_when_the_content_map_refuses() -> None:
    channel, state, tools, sent = make()

    answer = with_reply(
        tools["read_macro"],
        lambda: refuse_macro(
            channel,
            sent,
            MessageType.MACRO_READ,
            "MACRO_READ references an unknown macro: deal_a_card",
        ),
        name="deal_a_card",
    )

    assert "unknown macro" in answer
    assert "This run knows: none yet" in answer
    assert state.macros.registered("deal_a_card") is None


def test_read_macro_says_so_when_the_content_map_does_not_answer() -> None:
    """구버전 orchestration 이 `MACRO_READ` 를 떨어뜨리는 경로다.

    없는 것과 못 물어본 것은 다음에 할 일이 다르므로 문장을 가른다. 읽기는 다시 물어도
    아무것도 두 번 적히지 않으니 둘 다 런을 깨지 않는다.
    """
    _, state, tools, sent = make()

    answer = call(tools["read_macro"], name="deal_a_card")

    assert "did not answer" in answer
    assert len(macro_frames(sent, MessageType.MACRO_READ)) == 1
    assert state.macros.registered("deal_a_card") is None


def test_read_macro_refuses_a_stored_definition_that_no_longer_parses() -> None:
    """저장된 뒤에 허용 목록이 좁아질 수 있다. 실행 전에 걸리는 것이 맞다."""
    channel, state, tools, sent = make()
    broken = "def deal_a_card(card: object) -> None:\n    smash(card)\n"

    answer = with_reply(
        tools["read_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_READ,
            MessageType.MACRO_READ_RESULT,
            {**LOADED, "source": broken},
        ),
        name="deal_a_card",
    )

    assert "no longer parses" in answer
    assert state.macros.registered("deal_a_card") is None


# --- 지난 런이 등록한 것을 이번 런이 부른다 --------------------------------------


def test_run_macro_calls_a_macro_this_run_never_registered() -> None:
    channel, _, tools, sent = make()
    with_cards(channel)

    async def drive() -> str:
        answer_macro(
            channel,
            sent,
            MessageType.MACRO_READ,
            MessageType.MACRO_READ_RESULT,
            LOADED,
        )
        game = asyncio.create_task(_answer_one_action(channel, sent))
        answer = await tools["run_macro"].ainvoke(
            {
                "step": 1,
                "thought": "replaying what an earlier run worked out",
                "name": "deal_a_card",
                "arguments": {"card": "Root[0]/Hand[2]/Card(Clone)[3]"},
            }
        )
        game.cancel()
        return answer

    answer = asyncio.run(drive())

    assert "ran to the end" in answer
    assert len(macro_frames(sent, MessageType.MACRO_READ)) == 1
    assert len(actions(sent)) == 1


def test_run_macro_sends_nothing_to_the_game_when_the_lookup_comes_back_empty() -> None:
    channel, _, tools, sent = make()
    with_cards(channel)

    answer = with_reply(
        tools["run_macro"],
        lambda: refuse_macro(
            channel,
            sent,
            MessageType.MACRO_READ,
            "MACRO_READ references an unknown macro: nothing",
        ),
        name="nothing",
        arguments={},
    )

    assert "no registered macro called nothing" in answer
    assert actions(sent) == []


def test_run_macro_carries_on_when_the_content_map_does_not_answer() -> None:
    """구버전 orchestration 에서도 런이 멀쩡히 돈다. tool 이 문장을 돌려주고 끝난다."""
    channel, _, tools, sent = make()
    with_cards(channel)

    answer = call(tools["run_macro"], name="nothing", arguments={})

    assert "did not answer" in answer
    assert "Registered in this run: none yet" in answer
    assert actions(sent) == []


def test_run_macro_does_not_ask_the_content_map_about_a_draft() -> None:
    """초안이 있으면 그것을 등록하라고 말한다. 저쪽에 물을 일이 아니다."""
    _, _, tools, sent = drafted()

    answer = call(tools["run_macro"], name="deal_a_card", arguments={})

    assert "only a draft" in answer
    assert macro_frames(sent, MessageType.MACRO_READ) == []


# --- 남의 답으로 tool 을 풀지 않는다 ---------------------------------------------


def test_an_answer_echoing_another_request_type_is_dropped() -> None:
    """`type` 을 안 보면 남의 답이 이 tool 을 풀어 준다.

    지금은 응답 타입이 쓰기와 읽기로 갈려 있어 어긋날 길이 없지만, 저쪽이 macro 쓰기
    타입을 하나 더하면 그때 조용히 틀리는 자리가 여기다.
    """
    channel, state, tools, sent = drafted()

    answer = with_reply(
        tools["register_macro"],
        lambda: answer_macro(
            channel,
            sent,
            MessageType.MACRO_REGISTER,
            MessageType.MACRO_WRITE_RESULT,
            {**STORED, "type": "MACRO_READ"},
        ),
        name="deal_a_card",
    )

    # 버려졌으므로 tool 은 타임아웃으로 "확인할 수 없다" 에 도달한다. 그것이 실제 상황이다.
    assert "cannot be confirmed" in answer
    assert state.macros.registered("deal_a_card") is not None


async def _answer_one_action(channel, sent: list[dict]) -> None:
    """게임이 하는 대로 batch 하나에 `ACTION_RESULT` 하나."""
    for _ in range(4000):
        if actions(sent):
            channel.on_action_result(
                {
                    "correlationId": actions(sent)[0]["messageId"],
                    "payload": {"results": [], "frame": 10},
                }
            )
            return
        await asyncio.sleep(0)
