"""macro 정의 하나가 batch 여러 개로 쪼개져 도는가, 그리고 어디까지 갔다고 말하는가.

재는 것의 중심은 `applied`·`pending`·`skipped` 세 목록이다. `require` 가 실패했을 때
이미 게임에 나간 action 은 되돌릴 수 없으므로, 어디까지 갔는지가 틀리면 agent 가 macro
를 처음부터 다시 부르고 이미 슬롯에 올라간 카드 때문에 두 번째 시도가 다른 이유로 또
실패한다.
"""

import asyncio

import pytest

from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.macro.errors import (
    ACTION_REJECTED,
    REQUIRE_FAILED,
    SCENE_MISMATCH,
    SELECTOR_NOT_FOUND,
)
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.runner import run_macro
from app.qa.envelope import JsonRpcAction
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory

# 이슈의 예시. statement 아홉이 require 셋·대입 둘·action 넷으로 갈리고, 여섯 번째가
# `require` 다. 그것이 실패하면 `applied` 가 4-5, `pending` 이 7-9 여야 한다.
EXAMPLE = '''
def attack_with_combined_card(card_a: object, card_b: object) -> None:
    require(scene() == "Battle", "Open the battle screen before calling this.")
    zone: object = find(name="CombineZone")
    enemy: object = find(name="Enemy")
    drag(card_a, zone)
    drag(card_b, zone)
    require(
        actionable(selector("Root[0]/Canvas[1]/Combine[2]")),
        "Combine both cards before attacking.",
    )
    click(selector("Root[0]/Canvas[1]/Combine[2]"))
    click(selector("Root[0]/Canvas[1]/Attack[3]"))
    require(text(enemy) != "100", "Attack again; the enemy took no damage.")
'''


class FakeHost:
    """`run` 과 `memories` 둘만 하는 host. 가짜 channel 을 세울 일이 없다.

    `MacroHost` 가 protocol 인 덕이다 — runner 가 `ToolContext` 를 받으면 이 테스트가
    channel 과 socket 을 세워야 하고, 그러면 재는 것이 `applied` 가 아니라 wiring 이 된다.
    """

    def __init__(self, memory: SceneMemory) -> None:
        self.memory = memory
        self.sent: list[tuple[list[JsonRpcAction], str, int]] = []

    async def run(self, actions, summary: str, step: int) -> str:
        self.sent.append((actions, summary, step))
        return f"  {actions[0].method}: ok"

    def memories(self) -> MacroMemories:
        return MacroMemories(scene=self.memory)

    def methods(self) -> list[list[str]]:
        """배치마다 어떤 method 가 나갔나. 배치 경계를 보는 자리다."""
        return [[action.method for action in actions] for actions, _, _ in self.sent]

    def summaries(self) -> list[str]:
        return [summary for _, summary, _ in self.sent]


def scene_with(*objects: dict, statics: list[dict] | None = None, scene: str = "Battle") -> SceneMemory:
    memory = SceneMemory()
    memory.pulse.apply(
        PulseReading.model_validate(
            {
                "scene": scene,
                "whole": True,
                "active": list(objects),
                "statics": statics or [],
            }
        )
    )
    return memory


def card(
    selector: str,
    text: str | None = None,
    instance_id: int | None = 1,
    offers: dict | None = None,
    members: list[dict] | None = None,
) -> dict:
    return {
        "selector": selector,
        "id": instance_id,
        "text": text,
        "offers": offers,
        "members": members or [],
    }


def battle(combine_actionable: bool = True, enemy_text: str = "60") -> SceneMemory:
    return scene_with(
        card("Root[0]/Hand[2]/Card(Clone)[3]", text="shoot", instance_id=41),
        card("Root[0]/Hand[2]/Card(Clone)[4]", text="slash", instance_id=42),
        card("Root[0]/Canvas[1]/CombineZone[9]", instance_id=50),
        card(
            "Root[0]/Canvas[1]/Combine[2]",
            instance_id=51,
            offers={"clicks": ["onClick"]} if combine_actionable else None,
        ),
        card("Root[0]/Canvas[1]/Attack[3]", instance_id=52, offers={"clicks": ["onClick"]}),
        card("Root[0]/Enemy[7]", text=enemy_text, instance_id=60),
    )


def drive(host: FakeHost, source: str, name: str, arguments: dict, step: int = 2):
    definition = macro_definition_from_source(name, source)
    return asyncio.run(run_macro(host, definition, arguments, step))


def two_cards(host: FakeHost) -> dict:
    from app.agents.qa.macro.binding import MacroScope, resolve_find
    from app.agents.qa.macro.model import MacroFindValue, MacroStringLiteral

    def found(label: str):
        return resolve_find(
            host.memories(),
            MacroScope(),
            MacroFindValue(label=MacroStringLiteral(value=label)),
        )

    return {"card_a": found("shoot"), "card_b": found("slash")}


# --- 정상 경로 ----------------------------------------------------------------


def test_the_example_runs_to_the_end_with_one_batch_per_action_statement() -> None:
    """action statement 하나가 batch 하나다.

    같은 batch 안의 action 은 실패해도 끝까지 가므로, 둘을 한 batch 에 담으면 뒤엣것을
    보낸 뒤에야 실패를 알게 되어 `pending` 이 거짓말이 된다.
    """
    host = FakeHost(battle())
    result = drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert result.passed, result.failure
    # action 넷, batch 넷. drag 은 네 줄, click 은 세 줄이 한 배치에 탄다.
    assert host.methods() == [
        ["move_mouse", "mouse_down", "move_mouse", "mouse_up"],
        ["move_mouse", "mouse_down", "move_mouse", "mouse_up"],
        ["move_mouse", "mouse_down", "mouse_up"],
        ["move_mouse", "mouse_down", "mouse_up"],
    ]
    assert [place.number for place in result.applied] == [4, 5, 7, 8]
    assert all(place.function == "attack_with_combined_card" for place in result.applied)
    assert all(place.total == 9 for place in result.applied)
    assert result.pending == [] and result.skipped == []


def test_only_action_statements_reach_applied() -> None:
    """`require`·대입·`if`·`flag`·`ask_verdict` 는 게임에 아무것도 안 보낸다. 예외 없음."""
    host = FakeHost(battle())
    result = drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert len(result.applied) == len(host.sent) == 4


def test_the_statement_text_rides_along_as_the_action_summary() -> None:
    """`ACTION` frame 마다 그 statement 가 남는다.

    `ast.unparse` 를 거친 글이라 원문과 글자가 다를 수 있다 — 따옴표가 작은따옴표로
    바뀐다. 기대값이 그것을 반영한다.
    """
    host = FakeHost(battle())
    drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert host.summaries() == [
        "drag(card_a, zone)",
        "drag(card_b, zone)",
        "click(selector('Root[0]/Canvas[1]/Combine[2]'))",
        "click(selector('Root[0]/Canvas[1]/Attack[3]'))",
    ]


def test_the_step_the_macro_was_called_on_rides_on_every_batch() -> None:
    """macro 안의 statement 가 시나리오 step 을 따로 가질 길이 없다.

    `thought` 가 `run_macro` 호출 하나당 하나뿐인 것과 같은 사정이다.
    """
    host = FakeHost(battle())
    drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host), step=7)

    assert [step for _, _, step in host.sent] == [7, 7, 7, 7]


def test_a_selector_aims_with_the_selector_rather_than_the_id() -> None:
    """SDK 가 action 이 실제로 도는 순간에 푸므로, 움직인 대상도 지금 자리에서 맞는다."""
    host = FakeHost(battle())
    drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))
    aimed = host.sent[2][0][0]

    assert aimed.method == "move_mouse"
    assert aimed.params == ["Root[0]/Canvas[1]/Combine[2]"]


# --- 실패 지점 ----------------------------------------------------------------


def test_the_sixth_require_failing_splits_applied_from_pending_exactly() -> None:
    """이슈가 숫자로 적어 둔 경우다. `applied` 4-5, `pending` 7-9, `skipped` 빈 목록."""
    host = FakeHost(battle(combine_actionable=False))
    result = drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert not result.passed
    assert result.failure.code == REQUIRE_FAILED
    assert [place.number for place in result.applied] == [4, 5]
    assert [place.number for place in result.pending] == [7, 8, 9]
    assert result.skipped == []
    assert str(result.stopped_at) == "attack_with_combined_card step 6 of 9"
    # 실패한 statement 는 `applied` 에도 `pending` 에도 없다. 닿았고 안 나갔다.
    assert 6 not in [place.number for place in result.applied + result.pending]


def test_the_remedy_rides_in_the_failure_the_agent_reads() -> None:
    """무엇이 틀렸는지만 있고 어떻게 하라는 말이 없으면 읽는 쪽이 추측한다."""
    host = FakeHost(battle(combine_actionable=False))
    result = drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert "Combine both cards before attacking." in result.failure.reason
    assert result.failure.payload["observed"]


def test_a_failure_before_any_action_is_a_scene_mismatch() -> None:
    """들어선 자리가 틀린 것과 하다가 어긋난 것은 agent 가 할 일이 다르다."""
    host = FakeHost(battle())
    host.memory.pulse.scene = "Lobby"
    result = drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert result.failure.code == SCENE_MISMATCH
    assert result.applied == []
    assert host.sent == []


def test_a_find_that_matches_nothing_is_passed_up_unchanged() -> None:
    """`find` 세 코드는 `macro-find-binding` 이 낸 것을 그대로 상위로 전달한다."""
    host = FakeHost(battle())
    result = drive(
        host,
        'def m() -> None:\n    zone: object = find(name="Nowhere")\n    click(zone)\n',
        "m",
        {},
    )

    assert result.failure.code == SELECTOR_NOT_FOUND
    assert result.applied == []
    assert host.sent == []


# --- `if` 와 `skipped` ---------------------------------------------------------

BRANCHY = '''
def m(card: object) -> None:
    if actionable(card):
        click(card)
        flag("the card was pressable")
    else:
        move_pointer(card)
        ask_verdict(4, "a card that cannot be pressed should say why")
    press_key("Space", 0.1)
'''


def test_the_branch_not_taken_lands_in_skipped_and_not_in_pending() -> None:
    """실패해서 못 나간 것과 조건이 거짓이라 원래 안 가는 것은 다른 얘기다."""
    host = FakeHost(battle())
    pressable = two_cards(host)["card_a"]
    # 그 카드를 누를 수 있게 둔다.
    host.memory.pulse.held[pressable.key].offers = {"clicks": ["onClick"]}
    result = drive(host, BRANCHY, "m", {"card": pressable})

    assert result.passed, result.failure
    # 번호: 1 `if`, 2 click, 3 flag, 4 move_pointer, 5 ask_verdict, 6 press_key.
    assert [place.number for place in result.applied] == [2, 6]
    assert [place.number for place in result.skipped] == [4, 5]
    assert result.pending == []


def test_the_branch_that_is_taken_reaches_applied() -> None:
    host = FakeHost(battle())
    quiet = two_cards(host)["card_a"]
    host.memory.pulse.held[quiet.key].offers = None
    result = drive(host, BRANCHY, "m", {"card": quiet})

    assert result.passed, result.failure
    assert [place.number for place in result.applied] == [4, 6]
    assert [place.number for place in result.skipped] == [2, 3]


def test_a_flag_and_a_verdict_request_are_collected_apart() -> None:
    """하나는 알림이고 하나는 의무라 agent 가 다르게 다뤄야 한다."""
    host = FakeHost(battle())
    pressable = two_cards(host)["card_a"]
    host.memory.pulse.held[pressable.key].offers = {"clicks": ["onClick"]}
    result = drive(host, BRANCHY, "m", {"card": pressable})

    assert [one.message for one in result.flags] == ["the card was pressable"]
    assert result.verdict_requests == []

    quiet = two_cards(host)["card_b"]
    host.memory.pulse.held[quiet.key].offers = None
    other = drive(host, BRANCHY, "m", {"card": quiet})

    assert other.flags == []
    assert [one.step for one in other.verdict_requests] == [4]
    assert other.verdict_requests[0].expected.startswith("a card that cannot")


def test_a_flag_or_verdict_request_in_the_branch_not_taken_is_not_collected() -> None:
    host = FakeHost(battle())
    pressable = two_cards(host)["card_a"]
    host.memory.pulse.held[pressable.key].offers = {"clicks": ["onClick"]}
    result = drive(host, BRANCHY, "m", {"card": pressable})

    assert len(result.flags) == 1 and result.verdict_requests == []


def test_observed_is_the_reader_values_of_the_enclosing_if_conditions() -> None:
    """`if` 가 무엇을 읽을지 정하고 `flag` 나 `ask_verdict` 가 그것을 알린다."""
    host = FakeHost(battle())
    source = (
        'def m(enemy: object) -> None:\n'
        '    if text(enemy) == "0":\n'
        '        flag("the enemy is down")\n'
    )
    enemy = _found(host, "0")
    result = drive(host, source, "m", {"enemy": enemy})

    assert result.flags[0].observed == {"text(enemy)": "0"}


def test_nested_ifs_put_every_layer_of_reader_values_on_observed() -> None:
    host = FakeHost(battle())
    source = (
        'def m(enemy: object) -> None:\n'
        '    if text(enemy) == "0":\n'
        "        if exists(enemy):\n"
        '            ask_verdict(5, "a defeated enemy should leave the field")\n'
    )
    result = drive(host, source, "m", {"enemy": _found(host, "0")})

    assert result.verdict_requests[0].observed == {
        "text(enemy)": "0",
        "exists(enemy)": True,
    }


def test_a_statement_with_no_enclosing_if_still_stands_with_an_empty_observed() -> None:
    host = FakeHost(battle())
    result = drive(
        host, 'def m() -> None:\n    flag("the macro ran")\n', "m", {}
    )

    assert [one.message for one in result.flags] == ["the macro ran"]
    assert result.flags[0].observed == {}


def _found(host: FakeHost, enemy_text: str):
    from app.agents.qa.macro.binding import MacroScope, resolve_find
    from app.agents.qa.macro.model import MacroFindValue, MacroStringLiteral

    host.memory = battle(enemy_text=enemy_text)
    return resolve_find(
        host.memories(),
        MacroScope(),
        MacroFindValue(name=MacroStringLiteral(value="Enemy")),
    )


# --- helper -------------------------------------------------------------------

WITH_HELPER = '''
def attack(card: object) -> None:
    aim(card)
    press_key("Return", 0.1)

def aim(card: object) -> None:
    move_pointer(card)
    require(actionable(card), "Pick a card the game lets you press.")
    click(card)
'''


def test_a_helper_runs_as_a_call_and_its_numbers_are_its_own() -> None:
    """세는 것만 편다, 실행은 안 편다. `step N of M` 은 `def` 마다 1부터 센다."""
    host = FakeHost(battle())
    pressable = two_cards(host)["card_a"]
    host.memory.pulse.held[pressable.key].offers = {"clicks": ["onClick"]}
    result = drive(host, WITH_HELPER, "attack", {"card": pressable})

    assert result.passed, result.failure
    assert [(place.function, place.number, place.total) for place in result.applied] == [
        ("aim", 1, 3),
        ("aim", 3, 3),
        ("attack", 2, 2),
    ]


def test_a_require_failing_inside_a_helper_names_the_def_and_the_call_chain() -> None:
    """payload 가 어느 `def` 의 몇 번인지와 호출 사슬을 함께 싣는다."""
    host = FakeHost(battle())
    quiet = two_cards(host)["card_a"]
    host.memory.pulse.held[quiet.key].offers = None
    result = drive(host, WITH_HELPER, "attack", {"card": quiet})

    assert result.failure.code == REQUIRE_FAILED
    assert [str(place) for place in result.chain] == [
        "attack step 1 of 2",
        "aim step 2 of 3",
    ]
    assert str(result.stopped_at) == "aim step 2 of 3"
    # 안쪽의 남은 것 먼저, 그 다음 그것을 부른 쪽의 남은 것.
    assert [str(place) for place in result.pending] == [
        "aim step 3 of 3",
        "attack step 2 of 2",
    ]
    assert [str(place) for place in result.applied] == ["aim step 1 of 3"]
    assert result.failure.payload["chain"] == [str(one) for one in result.chain]


def test_a_flag_inside_a_helper_says_which_def_it_stood_in() -> None:
    host = FakeHost(battle())
    source = (
        "def attack(card: object) -> None:\n"
        "    look(card)\n"
        "\n"
        "def look(card: object) -> None:\n"
        "    if actionable(card):\n"
        '        flag("pressable")\n'
    )
    pressable = two_cards(host)["card_a"]
    host.memory.pulse.held[pressable.key].offers = {"clicks": ["onClick"]}
    result = drive(host, source, "attack", {"card": pressable})

    assert result.flags[0].place.function == "look"
    assert result.flags[0].place.number == 2


# --- `enter_text` 의 instance id -----------------------------------------------


def test_enter_text_takes_the_instance_id_out_of_the_bound_record() -> None:
    host = FakeHost(
        scene_with(card("Root[0]/Field[1]", text="", instance_id=77, offers={"clicks": []}))
    )
    result = drive(
        host,
        'def m() -> None:\n'
        '    field: object = find(name="Field")\n'
        '    enter_text(field, "hello")\n',
        "m",
        {},
    )

    assert result.passed, result.failure
    assert host.sent[0][0][0].method == "enter_text"
    assert host.sent[0][0][0].params == [77, "hello"]


def test_enter_text_is_refused_before_anything_is_sent_when_the_record_has_no_id() -> None:
    """`PulseObject.id` 는 `int | None` 이다. `None` 이면 넣을 값이 없다."""
    host = FakeHost(scene_with(card("Root[0]/Field[1]", text="", instance_id=None)))
    result = drive(
        host,
        'def m() -> None:\n'
        '    field: object = find(name="Field")\n'
        '    enter_text(field, "hello")\n',
        "m",
        {},
    )

    assert result.failure.code == ACTION_REJECTED
    assert host.sent == []
    assert result.applied == []


# --- 기본값 -------------------------------------------------------------------


def test_a_tool_default_the_author_did_not_write_comes_from_the_grammar() -> None:
    """저장된 JSON 은 저자가 적은 것만 든다. 기본값은 표가 낸다."""
    host = FakeHost(battle())
    card_a = two_cards(host)["card_a"]
    drive(host, "def m(card: object) -> None:\n    click(card)\n", "m", {"card": card_a})

    assert [action.params for action in host.sent[0][0]] == [
        ["Root[0]/Hand[2]/Card(Clone)[3]"],
        [0],
        [0],
    ]


def test_a_written_button_overrides_the_default() -> None:
    host = FakeHost(battle())
    card_a = two_cards(host)["card_a"]
    drive(
        host,
        "def m(card: object) -> None:\n    click(card, button=1)\n",
        "m",
        {"card": card_a},
    )

    assert host.sent[0][0][1].params == [1]


# --- 상한은 여기서 세지 않는다 --------------------------------------------------


def test_the_runner_has_no_statement_or_depth_limit_of_its_own() -> None:
    """상한은 `register_macro` 가 저장 시점에 끝낸다.

    런타임에 걸리면 이미 나간 action 은 되돌릴 수 없으므로, 여기에는 상한에 걸려 멈추는
    경로도 그 전용 코드도 없다. 상한에 꼭 닿는 macro 가 끝까지 도는 것이 그 증거다.
    """
    host = FakeHost(battle())
    card_a = two_cards(host)["card_a"]
    body = "".join("    click(card)\n" for _ in range(128))
    result = drive(host, f"def m(card: object) -> None:\n{body}", "m", {"card": card_a})

    assert result.passed
    assert len(result.applied) == 128
    assert len(host.sent) == 128
