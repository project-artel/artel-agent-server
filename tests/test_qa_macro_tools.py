"""macro tool 다섯. 초안의 수명주기와 호출 인자 검사.

이 층에만 있는 규칙들을 재는 자리다. 고치려면 먼저 읽어야 하고, `old_text` 는 유일해야
하고, 등록된 것은 고치는 중에도 멀쩡해야 하고, 갱신이 `screen` 관계를 지우지 않아야
하고, 호출 인자의 좌표와 `#` id 는 게임에 아무것도 보내기 전에 거절되어야 한다.
"""

import asyncio

import pytest

from app.agents.qa.arch import (
    PhaseCycleMode,
    QaArchSpec,
    VisionMode,
    default_resolved_arch,
    resolve_arch,
)
from app.agents.qa.tools import macro_tools
from app.agents.qa.macro.errors import COMPARISON_REJECTED, MACRO_NODE_REJECTED
from app.agents.qa.macro.grammar import TOOL_NAMES
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import MessageType
from app.llm.models import LLMModel
from app.qa.pulse import PULSE_VIEW_START, PulseReading

SIMPLE = '''def deal_a_card(card: object) -> None:
    require(exists(card), "Draw a card first.")
    slot: object = selector("Root[0]/Canvas[1]/Slot[4]")
    drag(card, slot)
'''


async def _discard(frame: dict) -> None:
    return None


def make(total_steps: int = 3, timeout: float = 0.05):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(
        qa_try_id=9, send=send, action_timeout=timeout, write_timeout=timeout
    )
    state = QaRunState(total_steps=total_steps)
    # phase cycle 을 끄고 만든다. 이 파일은 macro 가 무엇을 하는지를 재고, macro tool 이
    # 어느 phase 에서 불릴 수 있는지는 `tests/test_qa_phase_cycle.py` 가 잰다. 켜 두면
    # `register_macro`(UPDATE_MEMORY)가 첫 phase 에서 거절당해 여기서 재려던 것이 안 돈다.
    without_phases = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.off), LLMModel.gpt_6_luna
    )
    tools = {one.name: one for one in build_tools(channel, state, without_phases)}
    return channel, state, tools, sent


def with_cards(channel: QaRunChannel, scene: str = "Battle") -> None:
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": scene,
                "whole": True,
                "active": [
                    {
                        "selector": "Root[0]/Hand[2]/Card(Clone)[3]",
                        "id": 41,
                        "text": "shoot",
                        "offers": {"clicks": [{"on": "Button", "method": "Deal"}]},
                    },
                    {
                        "selector": "Root[0]/Canvas[1]/Slot[4]",
                        "id": 52,
                        "offers": {"clicks": [{"on": "Button", "method": "Deal"}]},
                    },
                ],
            }
        )
    )


def settled(channel: QaRunChannel, screen_id: str = "12", scene: str = "Battle") -> None:
    """지도가 이 런에 어느 `screen` 에 서 있다고 말하게 한다 (ARTEL-668).

    `register_macro` 가 다는 관계가 이 값이다 — `scene` 이름이 아니라 `screen.id`.
    """
    channel.on_screen_settled(
        {
            "type": MessageType.SCREEN_SETTLED.value,
            "payload": {
                "scene": {"scene_id": "3", "name": scene},
                "current_screen": {"screen_id": screen_id, "name": "Board"},
            },
        }
    )


def macro_frames(sent: list[dict], message_type: MessageType) -> list[dict]:
    return [frame for frame in sent if frame["type"] == message_type.value]


def answer_macro(
    channel: QaRunChannel,
    sent: list[dict],
    message_type: MessageType,
    result_type: MessageType,
    payload: dict,
):
    """저쪽이 답하는 것처럼, 방금 나간 요청의 correlation 을 물고."""
    already = len(macro_frames(sent, message_type))

    async def reply() -> None:
        for _ in range(200):
            if len(macro_frames(sent, message_type)) > already:
                break
            await asyncio.sleep(0)
        frame = {
            "type": result_type.value,
            "correlationId": macro_frames(sent, message_type)[-1]["messageId"],
            "payload": payload,
        }
        if result_type is MessageType.MACRO_WRITE_RESULT:
            channel.on_macro_write_result(frame)
        else:
            channel.on_macro_read_result(frame)

    return asyncio.create_task(reply())


def refuse_macro(
    channel: QaRunChannel, sent: list[dict], message_type: MessageType, reason: str
):
    """저쪽이 correlation 붙은 `ERROR` 로 거절하는 것처럼."""
    already = len(macro_frames(sent, message_type))

    async def reply() -> None:
        for _ in range(200):
            if len(macro_frames(sent, message_type)) > already:
                break
            await asyncio.sleep(0)
        channel.on_error(
            {
                "type": MessageType.ERROR.value,
                "correlationId": macro_frames(sent, message_type)[-1]["messageId"],
                "payload": {"message": reason},
            }
        )

    return asyncio.create_task(reply())


def with_reply(tool, reply_factory, **arguments) -> str:
    """답하는 쪽을 함께 돌리며 tool 하나를 부른다.

    `call` 은 `asyncio.run` 하나라 답을 보낼 자리가 없다. 저쪽이 답하는 프레임을 끼우려면
    같은 loop 안에서 둘을 함께 돌려야 한다.
    """

    async def both() -> str:
        reply_factory()
        return await tool.ainvoke({"step": 1, "thought": "testing", **arguments})

    return asyncio.run(both())


def call(tool, **arguments) -> str:
    return asyncio.run(tool.ainvoke({"step": 1, "thought": "testing", **arguments}))


def actions(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.ACTION.value]


# --- write ---------------------------------------------------------------------


def test_write_macro_parses_and_says_so_without_registering() -> None:
    _, state, tools, sent = make()

    answer = call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    assert "draft" in answer and "register_macro" in answer
    assert state.macros.draft("deal_a_card") == SIMPLE
    assert state.macros.registered("deal_a_card") is None
    # 아무 frame 도 안 나간다. 파싱만 한다.
    assert sent == []


def test_write_macro_needs_a_def_named_after_the_macro() -> None:
    _, state, tools, _ = make()

    answer = call(tools["write_macro"], name="attack", source=SIMPLE)

    assert MACRO_NODE_REJECTED in answer
    assert "attack" in answer
    # 거절된 초안은 저장하지 않는다. 저장하면 `read_macro` 가 부를 수 없는 글을 초안이라고
    # 돌려주고, agent 는 그것을 고치면 된다고 읽는다.
    assert state.macros.draft("attack") is None


def test_write_macro_reports_the_parameters_run_macro_will_ask_for() -> None:
    _, _, tools, _ = make()

    answer = call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    assert "card: object" in answer


# --- read ----------------------------------------------------------------------


def test_read_macro_returns_the_draft_when_there_is_one() -> None:
    _, state, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(tools["read_macro"], name="deal_a_card")

    assert "draft" in answer
    assert SIMPLE in answer


def test_read_macro_returns_the_registered_source_when_there_is_no_draft() -> None:
    _, state, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)
    call(tools["register_macro"], name="deal_a_card")

    answer = call(tools["read_macro"], name="deal_a_card")

    assert "registered" in answer
    assert SIMPLE in answer


def test_read_macro_says_what_the_run_knows_when_the_name_is_unknown() -> None:
    _, _, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(tools["read_macro"], name="nothing_like_it")

    assert "deal_a_card" in answer


# --- edit ----------------------------------------------------------------------


def test_edit_macro_refuses_a_macro_this_run_has_not_read() -> None:
    """고치려면 먼저 읽어야 한다. 안 읽고 고치는 것은 무엇을 지우는지 모르고 지우는 것이다."""
    _, state, tools, _ = make()
    # 직접 넣는다 — `write_macro` 를 쓰면 쓴 것이 읽은 것으로 세어진다.
    state.macros.write("deal_a_card", SIMPLE)

    answer = call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="drag(card, slot)",
        new_text="double_click(card)",
    )

    assert "read_macro" in answer
    assert state.macros.draft("deal_a_card") == SIMPLE


def test_edit_macro_changes_the_draft_once_it_has_been_read() -> None:
    _, state, tools, _ = make()
    state.macros.write("deal_a_card", SIMPLE)
    call(tools["read_macro"], name="deal_a_card")

    answer = call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="drag(card, slot)",
        new_text="double_click(card)",
    )

    assert "parses" in answer
    assert "double_click(card)" in state.macros.draft("deal_a_card")
    assert "drag(card, slot)" not in state.macros.draft("deal_a_card")


def test_edit_macro_refuses_old_text_that_appears_no_times() -> None:
    _, state, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="press_key(\"Space\", 0.1)",
        new_text="click(card)",
    )

    assert "does not appear" in answer
    assert state.macros.draft("deal_a_card") == SIMPLE


def test_edit_macro_refuses_old_text_that_appears_more_than_once() -> None:
    """모델은 줄 번호를 틀리고, 틀린 줄 번호는 조용히 엉뚱한 줄을 고친다.

    그래서 문자열 치환이고, 유일하지 않으면 시끄럽게 실패한다.
    """
    _, state, tools, _ = make()
    twice = (
        "def twice(card: object) -> None:\n"
        "    click(card)\n"
        "    click(card)\n"
    )
    call(tools["write_macro"], name="twice", source=twice)

    answer = call(
        tools["edit_macro"],
        name="twice",
        old_text="click(card)",
        new_text="double_click(card)",
    )

    assert "2 times" in answer
    assert state.macros.draft("twice") == twice


def test_a_refused_edit_of_a_registered_macro_leaves_no_draft_behind() -> None:
    """거절은 아무 자취도 남기지 않는다.

    등록된 것을 초안으로 복사하는 것은 상태를 바꾸는 일이라, 거절로 끝날 호출이 그것을
    하고 나면 `read_macro` 가 등록된 것과 같은 글을 초안이라고 돌려준다 — agent 는
    그것을 아직 등록 안 된 것으로 읽는다.
    """
    channel, state, tools, _ = make()
    with_cards(channel)
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)
    call(tools["register_macro"], name="deal_a_card")
    call(tools["read_macro"], name="deal_a_card")

    for old_text, new_text in (
        ("press_key(\"Space\", 0.1)", "click(card)"),
        ("drag(card, slot)", "for _ in card:\n        click(card)"),
    ):
        answer = call(
            tools["edit_macro"],
            name="deal_a_card",
            old_text=old_text,
            new_text=new_text,
        )
        assert "not changed" in answer or "does not appear" in answer
        assert state.macros.draft("deal_a_card") is None
        assert "registered" in call(tools["read_macro"], name="deal_a_card")


def test_edit_macro_names_a_macro_that_does_not_exist_before_asking_if_it_was_read() -> None:
    """이름을 잘못 적은 호출이 "읽지 않았다" 는 답을 받으면 엉뚱한 것을 고치러 간다."""
    _, _, tools, _ = make()

    answer = call(
        tools["edit_macro"], name="typo", old_text="a", new_text="b"
    )

    assert "no macro called typo" in answer


def test_an_edit_that_breaks_the_macro_leaves_the_draft_alone() -> None:
    _, state, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="drag(card, slot)",
        new_text="for _ in card:\n        click(card)",
    )

    assert MACRO_NODE_REJECTED in answer
    assert state.macros.draft("deal_a_card") == SIMPLE


def test_editing_a_registered_macro_copies_it_into_a_draft_and_leaves_it_running() -> None:
    """고치는 중에 그 macro 를 부르는 런이 깨지지 않게 한다.

    등록된 것은 `register_macro` 가 성공할 때까지 멀쩡하다.
    """
    channel, state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)
    call(tools["register_macro"], name="deal_a_card")
    call(tools["read_macro"], name="deal_a_card")

    call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="drag(card, slot)",
        new_text="double_click(card)",
    )

    assert "double_click(card)" in state.macros.draft("deal_a_card")
    # 등록된 행은 그대로다.
    registered = state.macros.registered("deal_a_card")
    assert registered.definition.source == SIMPLE
    assert registered.definition.entry.statements[2].callee == "drag"


# --- register ------------------------------------------------------------------


def test_register_macro_makes_the_draft_callable_and_relates_it_to_the_screen() -> None:
    channel, state, tools, _ = make()
    with_cards(channel)
    settled(channel, screen_id="12")
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(tools["register_macro"], name="deal_a_card")

    assert "registered" in answer
    registered = state.macros.registered("deal_a_card")
    # `scene` 이름이 아니라 `screen.id` 다. 저쪽은 이 build 의 `content_map` 에 없는
    # 값을 통째로 거절한다.
    assert registered.screens == ("12",)
    # 등록이 끝나면 초안은 할 일을 다했다.
    assert state.macros.draft("deal_a_card") is None


def test_register_macro_leaves_the_relation_empty_when_the_screen_is_unknown() -> None:
    """빈 관계는 아직 어디서 쓸지 모른다는 뜻이고, 이 경우와 맞는다."""
    _, state, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(tools["register_macro"], name="deal_a_card")

    assert "no screen" in answer
    assert state.macros.registered("deal_a_card").screens == ()


def test_register_macro_ignores_a_screen_verdict_from_another_scene() -> None:
    """옆 `scene` 의 화면 번호를 달면 그 관계는 거짓이다.

    게임은 `Battle` 과 `Battle 2` 를 둘 다 가질 수 있다.
    """
    channel, state, tools, _ = make()
    with_cards(channel, scene="Battle")
    settled(channel, screen_id="12", scene="Shop")
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    call(tools["register_macro"], name="deal_a_card")

    assert state.macros.registered("deal_a_card").screens == ()


def test_register_macro_adds_the_screens_the_agent_names() -> None:
    channel, state, tools, _ = make()
    with_cards(channel)
    settled(channel, screen_id="12")
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    call(tools["register_macro"], name="deal_a_card", screens=["40", "41"])

    assert state.macros.registered("deal_a_card").screens == ("12", "40", "41")


def test_register_macro_refuses_a_screen_that_is_not_an_id() -> None:
    """저쪽의 거절은 등록 전체를 버린다 — `screens` 하나 때문에 macro 행도 안 적힌다.

    `scene` 이름을 적는 것이 흔한 실수라, 그 왕복 전에 무엇을 적어야 하는지 말한다.
    """
    channel, state, tools, sent = make()
    with_cards(channel)
    settled(channel, screen_id="12")
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = call(tools["register_macro"], name="deal_a_card", screens=["Shop"])

    assert "screen ids" in answer and "Shop" in answer
    # 아무것도 안 등록하고 아무 프레임도 안 보낸다. 초안은 그대로다.
    assert state.macros.registered("deal_a_card") is None
    assert state.macros.draft("deal_a_card") == SIMPLE
    assert macro_frames(sent, MessageType.MACRO_REGISTER) == []


def test_registering_again_updates_in_place_and_keeps_the_relations() -> None:
    """지우고 새로 넣지 않는다. `ON DELETE CASCADE` 로 관계 행이 같이 사라진다.

    그 관계는 agent 가 공들여 단 것이라 고칠 때마다 잃으면 안 된다.
    """
    channel, state, tools, _ = make()
    with_cards(channel)
    settled(channel, screen_id="12")
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)
    call(tools["register_macro"], name="deal_a_card", screens=["40"])

    call(tools["read_macro"], name="deal_a_card")
    call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="drag(card, slot)",
        new_text="double_click(card)",
    )
    call(tools["register_macro"], name="deal_a_card", screens=["41"])

    registered = state.macros.registered("deal_a_card")
    assert registered.definition.entry.statements[2].callee == "double_click"
    assert registered.screens == ("12", "40", "41")


def test_register_macro_refuses_a_draft_that_does_not_parse() -> None:
    """저장된 뒤에 처음 거절되는 것을 막는 것이 이 검사의 자리다."""
    _, state, tools, _ = make()
    broken = "def broken(card: object) -> None:\n    smash(card)\n"
    state.macros.write("broken", broken)

    answer = call(tools["register_macro"], name="broken")

    assert MACRO_NODE_REJECTED in answer
    for name in TOOL_NAMES:
        assert name in answer
    assert state.macros.registered("broken") is None
    # 초안은 그대로다. `edit_macro` 가 고칠 수 있다.
    assert state.macros.draft("broken") == broken


def test_register_macro_with_no_draft_says_where_a_draft_starts() -> None:
    _, _, tools, _ = make()

    assert "write_macro" in call(tools["register_macro"], name="nothing")


# --- run -----------------------------------------------------------------------


def answer_the_game(channel: QaRunChannel, sent: list[dict], count: int):
    """게임이 하는 대로 답한다. batch 마다 `ACTION_RESULT` 하나."""

    async def reply() -> None:
        replied = 0
        for _ in range(4000):
            if len(actions(sent)) > replied:
                channel.on_action_result(
                    {
                        "correlationId": actions(sent)[replied]["messageId"],
                        "payload": {"results": [], "frame": 10 + replied},
                    }
                )
                replied += 1
                if replied >= count:
                    return
            await asyncio.sleep(0)

    return reply


def run_registered(name: str, args: dict, total_actions: int = 1):
    """등록까지 해 두고 `run_macro` 를 부른다. 게임의 답까지 함께 돌린다."""
    channel, state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name=name, source=SIMPLE)
    call(tools["register_macro"], name=name)

    async def drive() -> str:
        replier = asyncio.create_task(
            answer_the_game(channel, sent, total_actions)()
        )
        answer = await tools["run_macro"].ainvoke(
            {"step": 1, "thought": "replaying", "name": name, "arguments": args}
        )
        replier.cancel()
        return answer

    return channel, state, sent, asyncio.run(drive())


def test_run_macro_replays_the_registered_definition() -> None:
    _, _, sent, answer = run_registered(
        "deal_a_card", {"card": "Root[0]/Hand[2]/Card(Clone)[3]"}
    )

    assert "ran to the end" in answer
    # action statement 하나가 batch 하나다. 이 macro 에는 `drag` 하나뿐이다.
    assert len(actions(sent)) == 1
    methods = [one["method"] for one in actions(sent)[0]["payload"]["actions"]]
    assert methods == ["move_mouse", "mouse_down", "move_mouse", "mouse_up"]


TWO_PRESSES = (
    "def two_presses() -> None:\n"
    '    press_key("A", 0.1)\n'
    '    press_key("B", 0.1)\n'
)


def test_a_macro_answer_carries_one_pulse_view_however_many_actions_it_sent() -> None:
    """action 마다 `pulse` view 를 붙이던 때는 action 30 개짜리 macro 의 답 하나에 view 가
    31 개 실렸다(L1 try 143). view 는 macro 가 끝난 자리에 하나만 붙고, action 줄에는
    결과만 남는다."""
    channel, _state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name="two_presses", source=TWO_PRESSES)
    call(tools["register_macro"], name="two_presses")

    async def drive() -> str:
        replier = asyncio.create_task(answer_the_game(channel, sent, 2)())
        answer = await tools["run_macro"].ainvoke(
            {"step": 1, "thought": "replaying", "name": "two_presses", "arguments": {}}
        )
        replier.cancel()
        return answer

    answer = asyncio.run(drive())

    assert "ran to the end. 2 action(s) reached the game." in answer
    assert len(actions(sent)) == 2
    assert answer.count(PULSE_VIEW_START) == 1
    # view 는 답의 끝, action 줄들 뒤에 있다.
    assert answer.index("What the game said:") < answer.index(PULSE_VIEW_START)


def test_a_hand_sent_action_still_answers_with_the_screen() -> None:
    """`screen=False` 는 macro host 만 쓴다. 손으로 보낸 action 의 답에는 view 가 붙는다."""
    channel, _state, tools, sent = make()
    with_cards(channel)

    async def drive() -> str:
        replier = asyncio.create_task(answer_the_game(channel, sent, 1)())
        answer = await tools["press_key"].ainvoke(
            {"step": 1, "key_code": "A", "duration_seconds": 0.1, "thought": "누른다"}
        )
        replier.cancel()
        return answer

    assert asyncio.run(drive()).count(PULSE_VIEW_START) == 1


def test_run_macro_refuses_a_coordinate_as_an_object_argument() -> None:
    """문법이 막는 것은 저장된 정의 안의 좌표뿐이고, 호출 인자는 따로 검사해야 한다."""
    _, _, sent, answer = run_registered("deal_a_card", {"card": "640,360"}, 0)

    assert "screen point" in answer or "instance id" in answer
    assert actions(sent) == []


def test_run_macro_refuses_a_hash_id_as_an_object_argument() -> None:
    _, _, sent, answer = run_registered("deal_a_card", {"card": "#41"}, 0)

    assert "instance id" in answer
    assert actions(sent) == []


def test_run_macro_refuses_a_missing_argument_before_anything_is_sent() -> None:
    _, _, sent, answer = run_registered("deal_a_card", {}, 0)

    assert "needs `card`" in answer
    assert actions(sent) == []


def test_run_macro_refuses_an_argument_the_macro_has_no_parameter_for() -> None:
    _, _, sent, answer = run_registered(
        "deal_a_card",
        {"card": "Root[0]/Hand[2]/Card(Clone)[3]", "speed": 2},
        0,
    )

    assert "speed" in answer
    assert actions(sent) == []


@pytest.mark.parametrize(
    "declared, given, ok",
    [
        ("int", 3, True),
        ("int", 3.5, False),
        ("int", True, False),
        ("float", 3, True),
        ("float", 3.5, True),
        ("string", "x", True),
        ("string", 3, False),
        ("bool", True, True),
        ("bool", 1, False),
    ],
)
def test_an_argument_is_checked_against_its_declared_type(
    declared: str, given, ok: bool
) -> None:
    """`bool` 을 먼저 가린다. Python 에서 `True` 는 int 이기도 하다."""
    channel, state, tools, sent = make()
    with_cards(channel)
    source = (
        f"def tuned(value: {declared}) -> None:\n"
        '    require(scene() == "Nowhere", "go somewhere else")\n'
    )
    call(tools["write_macro"], name="tuned", source=source)
    call(tools["register_macro"], name="tuned")

    answer = asyncio.run(
        tools["run_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": "tuned", "arguments": {"value": given}}
        )
    )

    # 이 macro 의 `require` 는 반드시 실패한다. 인자 검사를 통과했는지는 어느 쪽으로
    # 거절됐는지로 가린다.
    if ok:
        assert "SCENE_MISMATCH" in answer
    else:
        assert "declared" in answer and "nothing was sent to the game" in answer
    assert actions(sent) == []


def test_run_macro_will_not_call_a_draft() -> None:
    _, state, tools, _ = make()
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)

    answer = asyncio.run(
        tools["run_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": "deal_a_card", "arguments": {}}
        )
    )

    assert "register_macro" in answer


def test_run_macro_calls_the_registered_one_even_while_a_draft_exists() -> None:
    """고치는 중에도 `run_macro` 는 등록된 것을 부른다."""
    channel, state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name="deal_a_card", source=SIMPLE)
    call(tools["register_macro"], name="deal_a_card")
    call(tools["read_macro"], name="deal_a_card")
    call(
        tools["edit_macro"],
        name="deal_a_card",
        old_text="drag(card, slot)",
        new_text="double_click(card)",
    )

    async def drive() -> str:
        replier = asyncio.create_task(answer_the_game(channel, sent, 1)())
        answer = await tools["run_macro"].ainvoke(
            {
                "step": 1,
                "thought": "t",
                "name": "deal_a_card",
                "arguments": {"card": "Root[0]/Hand[2]/Card(Clone)[3]"},
            }
        )
        replier.cancel()
        return answer

    asyncio.run(drive())

    methods = [one["method"] for one in actions(sent)[0]["payload"]["actions"]]
    # 등록된 것은 `drag` 이다. 초안의 `double_click` 이 아니다.
    assert methods == ["move_mouse", "mouse_down", "move_mouse", "mouse_up"]


# --- macro 가 macro 를 부르지 못한다 -------------------------------------------


def test_a_macro_cannot_call_run_macro_or_any_other_macro_tool() -> None:
    """v1 에서 재귀를 허용하지 않는다. parser 가 tool 열넷과 helper 만 받는다."""
    _, _, tools, _ = make()

    for callee in ("run_macro", "write_macro", "register_macro", "read_macro"):
        answer = call(
            tools["write_macro"],
            name="nested",
            source=f'def nested() -> None:\n    {callee}("other")\n',
        )
        assert MACRO_NODE_REJECTED in answer


# --- `ask_verdict` 와 `finish_run` ---------------------------------------------


def test_a_step_a_macro_asked_about_still_has_to_be_reported() -> None:
    """새 강제 장치를 만들지 않는다. `finish_run` 이 이미 강제한다.

    `ask_verdict` 가 청하는 step 은 `run_macro` 를 부른 시나리오 step 이고,
    `QaRunState.unreported_steps` 가 판정 없는 시나리오 step 을 세므로 거기 그대로 남는다.
    """
    channel, state, tools, sent = make(total_steps=3)
    with_cards(channel)
    source = (
        "def look() -> None:\n"
        '    if exists(selector("Root[0]/Hand[2]/Card(Clone)[3]")):\n'
        '        ask_verdict("a dealt card should be pressable")\n'
    )
    call(tools["write_macro"], name="look", source=source)
    call(tools["register_macro"], name="look")

    answer = asyncio.run(
        tools["run_macro"].ainvoke(
            {"step": 2, "thought": "t", "name": "look", "arguments": {}}
        )
    )

    assert "NEEDS A VERDICT" in answer
    # 부른 step 을 묻는다. macro 원문에는 step 이 없다.
    assert "scenario step 2" in answer
    assert "report_step" in answer
    # 그 step 은 아직 판정이 없다. `finish_run` 이 첫 시도를 되민다.
    assert 2 in state.unreported_steps()
    pushed_back = asyncio.run(
        tools["finish_run"].ainvoke(
            {"passed": True, "summary": "done", "thought": "t"}
        )
    )
    assert "no verdict" in pushed_back


def test_a_flag_only_macro_does_not_add_a_step_to_report() -> None:
    """`flag` 는 어느 step 도 지목하지 않으므로 `unreported_steps` 에 아무것도 더하지 않는다."""
    channel, state, tools, _ = make(total_steps=1)
    with_cards(channel)
    source = (
        "def look() -> None:\n"
        '    if exists(selector("Root[0]/Hand[2]/Card(Clone)[3]")):\n'
        '        flag("a card is on the table")\n'
    )
    call(tools["write_macro"], name="look", source=source)
    call(tools["register_macro"], name="look")
    before = state.unreported_steps()

    answer = asyncio.run(
        tools["run_macro"].ainvoke(
            {"step": 1, "thought": "t", "name": "look", "arguments": {}}
        )
    )

    assert "FLAG" in answer
    assert state.unreported_steps() == before


# --- 설명이 받을 수 있는 것을 전부 대는가 ---------------------------------------


def test_the_macro_tools_sit_beside_the_action_tools_in_the_offered_list() -> None:
    """목록의 순서가 계약이다. `run_config` 가 이 순서를 저장한다."""
    channel, state, _, _ = make()
    names = [one.name for one in build_tools(channel, state, default_resolved_arch())]

    assert names[names.index("reset_game") + 1 : names.index("wait_for_operator")] == [
        "write_macro",
        "edit_macro",
        "read_macro",
        "register_macro",
        "run_macro",
        "resume_macro",
        "decline_macro_draft",
    ]


# --- tool 다섯은 런을 죽이지 않는다 ----------------------------------------------
#
# 실제 런 하나가 여기서 죽었다. agent 가 `run_macro` 를 불렀고 macro 가
# `COMPARISON_REJECTED` 로 멈췄는데, 그 결과를 그리다 `'str' object has no attribute
# 'items'` 가 tool 밖으로 나가 런이 `run_incomplete` 로 끝났다.
#
# **macro 가 멈추는 것은 정상 동작이다.** tool 다섯이 있는 이유가 agent 가 그 실패를
# 읽고 `edit_macro` 로 고쳐 다시 등록하는 것인데, 실패가 런을 죽이면 고칠 기회가 안 온다.

# `member()` 가 돌려주는 모양은 저장 시점에 알 수 없다. 그래서 parser 가 통과시키고,
# 실제로 수가 도착하는 런타임에 `COMPARISON_REJECTED` 가 난다 — 비교 없는 조건은 bool
# 로 도착해야 한다.
COMPARED = '''def judge_the_card(card: object) -> None:
    require(member(card, "Enemy.Hp"), "Draw a card with a bigger number.")
'''


def with_a_watched_number(channel: QaRunChannel) -> None:
    """게임이 이 카드의 `Enemy.Hp` 를 지켜보고 있다고 해 둔다. 기본 빌드에서 수를 읽는 자리다."""
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": "Battle",
                "whole": True,
                "active": [
                    {
                        "selector": "Root[0]/Hand[2]/Card(Clone)[3]",
                        "id": 41,
                        "members": [{"on": "Game.Enemy", "member": "Hp", "value": 42}],
                    }
                ],
            }
        )
    )


def test_a_macro_stopped_by_comparison_rejected_is_drawn_without_killing_the_run() -> None:
    """실제로 터진 경로 그대로. `>` 는 string 에 못 쓰므로 `COMPARISON_REJECTED` 가 난다.

    `binding` 은 비교에 도착한 값 하나를 `arrived` 에, `runner` 는 조건이 읽은 값들을
    `observed` 에 싣는다. 종전에는 둘 다 `observed` 였고 그리는 쪽이 문자열에 대고
    `.items()` 를 불렀다.
    """
    channel, _, tools, _ = make()
    with_a_watched_number(channel)
    call(tools["write_macro"], name="judge_the_card", source=COMPARED)
    call(tools["register_macro"], name="judge_the_card")

    answer = call(
        tools["run_macro"],
        name="judge_the_card",
        arguments={"card": "Root[0]/Hand[2]/Card(Clone)[3]"},
    )

    assert COMPARISON_REJECTED in answer
    # 도착한 값이 그려진다. 문자열 칸이 dict 인 척하지 않는다.
    assert "The value that arrived there: 42" in answer
    # 게임에는 아무것도 안 갔다. `require` 가 첫 statement 다.
    assert "Reached the game" not in answer


def test_a_macro_tool_that_hits_a_server_defect_answers_instead_of_raising(
    monkeypatch,
) -> None:
    """tool 본문에서 새는 예외는 모델이 읽는 문장이 된다.

    `capability_tools._write_capability` 와 같은 규율이다 — macro tool 하나가 터졌다고
    시나리오가 멈추면 이 다섯은 런이 지는 위험이지 보태는 것이 아니다.
    """
    channel, _, tools, _ = make()
    with_a_watched_number(channel)
    call(tools["write_macro"], name="judge_the_card", source=COMPARED)
    call(tools["register_macro"], name="judge_the_card")

    def explode(_result):
        raise AttributeError("'str' object has no attribute 'items'")

    monkeypatch.setattr(macro_tools, "_render", explode)

    answer = call(
        tools["run_macro"],
        name="judge_the_card",
        arguments={"card": "Root[0]/Hand[2]/Card(Clone)[3]"},
    )

    # 예외가 밖으로 안 나갔고, 무엇이 잘못됐는지를 말한다.
    assert "run_macro" in answer and "error inside the server" in answer
    assert "defect in the server" in answer


def test_an_operator_ending_the_run_is_not_turned_into_a_sentence(monkeypatch) -> None:
    """`QaCancelled` 는 통과시킨다. operator 가 런을 끝낸 것이라 고칠 것이 없다."""
    channel, _, tools, _ = make()
    with_a_watched_number(channel)
    call(tools["write_macro"], name="judge_the_card", source=COMPARED)
    call(tools["register_macro"], name="judge_the_card")

    def give_up(_result):
        raise QaCancelled()

    monkeypatch.setattr(macro_tools, "_render", give_up)

    with pytest.raises(QaCancelled):
        call(
            tools["run_macro"],
            name="judge_the_card",
            arguments={"card": "Root[0]/Hand[2]/Card(Clone)[3]"},
        )


def test_every_macro_tool_body_is_guarded_against_a_leaking_exception() -> None:
    """다섯 **전부**다. 아래 `_render` 테스트가 재는 것은 `run_macro` 하나뿐이라,
    나중에 여섯 번째 tool 이 guard 없이 들어와도 그쪽은 조용히 통과한다.

    감싼 함수의 `__code__.co_name` 이 그 표시다. `functools.wraps` 가 이름과
    `__wrapped__` 는 베껴도 `__code__` 는 안 베끼므로, 이 칸만은 속지 않는다. tool 의
    이름과 argument schema 가 그대로인 것은 `tests/test_qa_arch.py` 의 fingerprint pin
    이 따로 지킨다 — langchain 이 `__wrapped__` 를 따라가 서명을 떠내기 때문이다.
    """
    # phase cycle 을 끄고 만든다. 켜면 `tools/__init__.py` 의 `_phase_gated` 가 모든 tool
    # 을 `guarded` 로 한 겹 더 감싸는데, 그 wrapper 는 `__wrapped__` 를 안 남겨서 맨 바깥만
    # 보이고 그 안의 이 guard 는 안 보인다. 재려는 것은 macro 본문의 guard 다.
    channel = QaRunChannel(qa_try_id=9, send=_discard, action_timeout=0.05, write_timeout=0.05)
    without_phases = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.off), LLMModel.gpt_6_luna
    )
    tools = {
        one.name: one
        for one in build_tools(channel, QaRunState(total_steps=3), without_phases)
    }

    for name in (
        "write_macro",
        "edit_macro",
        "read_macro",
        "register_macro",
        "run_macro",
        "resume_macro",
        "decline_macro_draft",
    ):
        assert tools[name].coroutine.__code__.co_name == "answering", name
        assert tools[name].name == name


# --- checkpoint 와 resume_macro (ARTEL-949) --------------------------------------
#
# 멈춘 macro 는 tool 호출 사이에 산다. 그래서 이 테스트들은 `run_macro` 와 `resume_macro`
# 를 **한 event loop 안에서** 부른다 — `call` 처럼 호출마다 `asyncio.run` 을 쓰면 멈춘 task
# 가 첫 loop 와 함께 닫힌다. 실제 런도 loop 하나에서 돈다.

CHECKED = (
    "def go() -> None:\n"
    '    press_key("A", 0.1)\n'
    '    checkpoint("look at the board")\n'
    '    press_key("B", 0.1)\n'
)


def registered_checked():
    channel, state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name="go", source=CHECKED)
    call(tools["register_macro"], name="go")
    return channel, state, tools, sent


def invoke(tool, **arguments):
    return tool.ainvoke({"step": 1, "thought": "testing", **arguments})


def test_run_macro_hands_the_turn_back_at_a_checkpoint_and_resume_carries_on() -> None:
    _channel, state, tools, sent = registered_checked()

    async def scenario():
        paused = await invoke(tools["run_macro"], name="go")
        sent_while_paused = len(actions(sent))
        carried_on = await invoke(tools["resume_macro"], proceed=True)
        return paused, sent_while_paused, carried_on

    paused, sent_while_paused, carried_on = asyncio.run(scenario())

    assert "PAUSED" in paused and "look at the board" in paused
    assert "not a failure" in paused
    assert "Not sent yet: go step 3 of 3" in paused
    # 멈춰 있는 동안에는 첫 action 하나만 나갔다.
    assert sent_while_paused == 1
    assert "ran to the end" in carried_on
    assert len(actions(sent)) == 2
    assert state.paused_macro is None


def test_resume_with_proceed_false_sends_nothing_after_the_checkpoint() -> None:
    _channel, state, tools, sent = registered_checked()

    async def scenario():
        await invoke(tools["run_macro"], name="go")
        return await invoke(tools["resume_macro"], proceed=False)

    stopped = asyncio.run(scenario())

    assert "as you asked" in stopped and "not a failure" in stopped
    assert len(actions(sent)) == 1
    assert state.paused_macro is None


def test_run_macro_is_refused_while_another_macro_is_paused() -> None:
    """둘을 같이 두면 게임을 모는 쪽이 둘이 된다."""
    _channel, state, tools, sent = registered_checked()

    async def scenario():
        await invoke(tools["run_macro"], name="go")
        refused = await invoke(tools["run_macro"], name="go")
        paused_after = state.paused_macro
        await invoke(tools["resume_macro"], proceed=False)
        return refused, paused_after

    refused, paused_after = asyncio.run(scenario())

    assert "paused at a checkpoint" in refused and "resume_macro" in refused
    assert paused_after is not None
    assert len(actions(sent)) == 1


def test_resume_refuses_a_step_other_than_the_one_the_macro_was_called_for() -> None:
    _channel, state, tools, _sent = registered_checked()

    async def scenario():
        await invoke(tools["run_macro"], name="go")
        refused = await tools["resume_macro"].ainvoke(
            {"step": 2, "thought": "t", "proceed": True}
        )
        still_paused = state.paused_macro is not None
        await invoke(tools["resume_macro"], proceed=False)
        return refused, still_paused

    refused, still_paused = asyncio.run(scenario())

    assert "step 1" in refused and "Nothing was sent" in refused
    assert still_paused


def test_resume_with_nothing_paused_says_so() -> None:
    _channel, _state, tools, _sent = make()

    assert "nothing to resume" in call(tools["resume_macro"], proceed=True)


def test_cancelling_the_call_that_waits_on_a_macro_stops_the_macro_too() -> None:
    """task 는 기다리는 쪽과 함께 끝난다. 안 그러면 런이 끝난 뒤에도 macro 가 혼자 보낸다."""
    from app.agents.qa.macro.session import MacroSession

    async def scenario():
        gate = asyncio.Event()
        sent_after: list[str] = []

        async def macro():
            await gate.wait()
            sent_after.append("sent")
            raise AssertionError("must not run")

        session = MacroSession("m", 1)
        session.start(macro())
        waiting = asyncio.create_task(session.settle())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        gate.set()
        await asyncio.sleep(0)
        return session.task.cancelled(), sent_after

    cancelled, sent_after = asyncio.run(scenario())

    assert cancelled
    assert sent_after == []
