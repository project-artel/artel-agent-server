"""macro 정의 하나가 batch 여러 개로 쪼개져 도는가, 그리고 어디까지 갔다고 말하는가.

재는 것의 중심은 `applied`·`pending`·`skipped` 세 목록이다. `require` 가 실패했을 때
이미 게임에 나간 action 은 되돌릴 수 없으므로, 어디까지 갔는지가 틀리면 agent 가 macro
를 처음부터 다시 부르고 이미 슬롯에 올라간 카드 때문에 두 번째 시도가 다른 이유로 또
실패한다.
"""

import asyncio

import pytest

from app.agents.qa.macro.binding import LateSelector, MacroMemories
from app.agents.qa.macro.errors import (
    ACTION_REJECTED,
    COMPARISON_REJECTED,
    REQUIRE_FAILED,
    SCENE_MISMATCH,
    SELECTOR_NOT_FOUND,
    SELECTOR_AMBIGUOUS,
    WAIT_TIMEOUT,
)
from app.agents.qa.macro.errors import LOOP_LIMIT, OPERATOR_INTERRUPTED, SCREEN_UNCHANGED
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.model import (
    MacroAssignStatement,
    MacroCheckpointStatement,
    MacroComparisonCondition,
    MacroDefinition,
    MacroFindValue,
    MacroFunction,
    MacroIfStatement,
    MacroLiteral,
    MacroLiteralArgument,
    MacroLiteralOperand,
    MacroNameArgument,
    MacroNameOperand,
    MacroParameter,
    MacroPredicateCondition,
    MacroReaderCall,
    MacroReaderOperand,
    MacroRequireStatement,
    MacroReturnStatement,
    MacroSelectorTarget,
    MacroStringLiteral,
    MacroWaitUntilStatement,
    MacroWhileStatement,
)
from app.agents.qa.macro.grammar import MacroOperator, MacroShape, MacroType
from app.agents.qa.macro.runner import MacroPulseWait, STILL_SCREENS_BEFORE_STOP, run_macro
from app.qa.acting import ActionOutcome, PressLanding, ScreenChange
from app.qa.channel import QaCancelled
from app.qa.envelope import JsonRpcAction
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory


def reached(text: str = "  ok") -> ActionOutcome:
    """누름이 닿았고 화면이 움직였다. 멈출 이유가 하나도 없는 결과."""
    return ActionOutcome(
        text=text, screen=ScreenChange.moved, landings=(PressLanding.sent,)
    )

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

    def __init__(
        self,
        memory: SceneMemory,
        answers: list[ActionOutcome] | None = None,
        decisions: list[bool] | None = None,
    ) -> None:
        self.memory = memory
        self.sent: list[tuple[list[JsonRpcAction], str, int]] = []
        # `checkpoint` 마다 이을지(`True`) 그만둘지. 다 쓰면 잇는다.
        self.decisions = list(decisions or [])
        # `checkpoint` 에서 멈춘 그 순간의 장부. 이어 돌면 runner 가 비우므로 사본을 둔다.
        self.paused: list[dict] = []
        # batch 하나에 답 하나. 다 쓰면 그 뒤는 기본값이다 — 128 개를 손으로 적지
        # 않으려는 것이고, 앞의 몇 개만 다르게 두는 테스트가 그 뒤를 안 적어도 된다.
        self.answers = list(answers or [])

    async def run(self, actions, summary: str, step: int) -> ActionOutcome:
        self.sent.append((actions, summary, step))
        answered = len(self.sent) - 1
        if answered < len(self.answers):
            return self.answers[answered]
        # 기본값은 **멈출 이유가 없는 결과**다. 누름이 닿았고 화면이 움직였다.
        return reached(f"  {actions[0].method}: ok")

    def memories(self) -> MacroMemories:
        return MacroMemories(scene=self.memory)

    async def wait_for_pulse(self, timeout: float) -> MacroPulseWait:
        raise AssertionError("this macro did not expect a pulse wait")

    async def checkpoint(self, result) -> bool:
        self.paused.append(
            {
                "at": str(result.paused_at),
                "reason": result.paused_reason,
                "applied": [str(one) for one in result.applied],
                "pending": [str(one) for one in result.pending],
                "sent": len(self.sent),
            }
        )
        return self.decisions.pop(0) if self.decisions else True

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
            offers={"clicks": [{"on": "Button", "method": "Deal"}]} if combine_actionable else None,
        ),
        card("Root[0]/Canvas[1]/Attack[3]", instance_id=52, offers={"clicks": [{"on": "Button", "method": "Deal"}]}),
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


def test_the_summary_is_the_line_the_author_wrote_character_for_character() -> None:
    """`ACTION` frame 마다 그 statement 가 **원문 그대로** 남는다.

    `ast.unparse` 로 되찍으면 따옴표가 작은따옴표로 바뀌고 주석이 떨어진다. 그러면
    timeline 에 남는 것이 agent 가 쓴 글자가 아니고, macro 를 쓴 agent 가 자기가 쓴 줄을
    못 알아본다.
    """
    host = FakeHost(battle())
    drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert host.summaries() == [
        "drag(card_a, zone)",
        "drag(card_b, zone)",
        'click(selector("Root[0]/Canvas[1]/Combine[2]"))',
        'click(selector("Root[0]/Canvas[1]/Attack[3]"))',
    ]
    # 그 줄이 실제로 원문에 그대로 있다. 기대값을 손으로 적어 맞춘 것이 아니다.
    for summary in host.summaries():
        assert summary in EXAMPLE


def test_a_trailing_comment_survives_into_the_summary() -> None:
    host = FakeHost(battle())
    source = (
        "def m(card: object) -> None:\n"
        '    click(card)  # 카드를 낸다\n'
    )
    drive(host, source, "m", {"card": two_cards(host)["card_a"]})

    assert host.summaries() == ["click(card)  # 카드를 낸다"]


def test_a_docstring_is_never_run() -> None:
    """docstring 은 statement 가 아니므로 batch 도, 번호도 받지 않는다 (ARTEL-984)."""
    host = FakeHost(battle())
    source = (
        "def m(card: object) -> None:\n"
        '    """Deal one card."""\n'
        "    click(card)\n"
    )
    result = drive(host, source, "m", {"card": two_cards(host)["card_a"]})

    assert result.passed, result.failure
    assert host.summaries() == ["click(card)"]
    assert [place.number for place in result.applied] == [1]
    assert all(place.total == 1 for place in result.applied)


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
        ask_verdict("a card that cannot be pressed should say why")
    press_key("Space", 0.1)
'''


def test_the_branch_not_taken_lands_in_skipped_and_not_in_pending() -> None:
    """실패해서 못 나간 것과 조건이 거짓이라 원래 안 가는 것은 다른 얘기다."""
    host = FakeHost(battle())
    pressable = two_cards(host)["card_a"]
    # 그 카드를 누를 수 있게 둔다.
    host.memory.pulse.held[pressable.key].offers = {"clicks": [{"on": "Button", "method": "Deal"}]}
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
    host.memory.pulse.held[pressable.key].offers = {"clicks": [{"on": "Button", "method": "Deal"}]}
    result = drive(host, BRANCHY, "m", {"card": pressable})

    assert [one.message for one in result.flags] == ["the card was pressable"]
    assert result.verdict_requests == []

    quiet = two_cards(host)["card_b"]
    host.memory.pulse.held[quiet.key].offers = None
    other = drive(host, BRANCHY, "m", {"card": quiet}, step=4)

    assert other.flags == []
    # 부른 step 이다. `BRANCHY` 원문에는 step 번호가 없다.
    assert [one.step for one in other.verdict_requests] == [4]
    assert other.verdict_requests[0].expected.startswith("a card that cannot")


def test_a_flag_or_verdict_request_in_the_branch_not_taken_is_not_collected() -> None:
    host = FakeHost(battle())
    pressable = two_cards(host)["card_a"]
    host.memory.pulse.held[pressable.key].offers = {"clicks": [{"on": "Button", "method": "Deal"}]}
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
        '            ask_verdict("a defeated enemy should leave the field")\n'
    )
    result = drive(host, source, "m", {"enemy": _found(host, "0")})

    assert result.verdict_requests[0].observed == {
        "text(enemy)": "0",
        "exists(enemy)": True,
    }


# --- 읽은 값의 bind ---------------------------------------------------------------


def enemy_with_hp(hp) -> dict:
    return card(
        "Root[0]/Enemy[7]",
        instance_id=60,
        offers={"clicks": [{"on": "Button", "method": "Hit"}]},
        members=[{"on": "Game.Enemy", "member": "Hp", "value": hp}],
    )


class HitsChangeTheGame(FakeHost):
    """action 이 하나 나가면 그 뒤로 화면이 `after` 가 된다. 눌렀더니 숫자가 바뀐 것이다."""

    def __init__(self, memory: SceneMemory, after: SceneMemory) -> None:
        super().__init__(memory)
        self.after = after

    async def run(self, actions, summary: str, step: int) -> ActionOutcome:
        outcome = await super().run(actions, summary, step)
        self.memory = self.after
        return outcome


ATTACK_AND_COMPARE = '''
def hit(enemy: object) -> None:
    before: float = member(enemy, "Enemy.Hp")
    click(enemy)
    require(member(enemy, "Enemy.Hp") < before, "The attack took no health.")
'''


def test_a_bound_reading_keeps_the_value_from_before_the_action() -> None:
    """다시 읽으면 `70 < 70` 이라 이 macro 는 실패한다. 통과하는 것이 그 값이 얼어 있다는 증거다."""
    host = HitsChangeTheGame(scene_with(enemy_with_hp(100)), scene_with(enemy_with_hp(70)))

    result = drive(host, ATTACK_AND_COMPARE, "hit", {"enemy": LateSelector(selector="Root[0]/Enemy[7]")})

    assert result.passed, result.failure


def test_a_bound_reading_lets_the_macro_catch_an_attack_that_did_nothing() -> None:
    """체력이 그대로면 `REQUIRE_FAILED` 다. 이것을 잡으려고 읽은 값을 bind 한다."""
    host = HitsChangeTheGame(scene_with(enemy_with_hp(100)), scene_with(enemy_with_hp(100)))

    result = drive(host, ATTACK_AND_COMPARE, "hit", {"enemy": LateSelector(selector="Root[0]/Enemy[7]")})

    assert not result.passed
    assert result.failure.code == REQUIRE_FAILED


def test_a_reading_that_arrives_as_another_shape_stops_at_the_bind() -> None:
    """뒤의 비교까지 가서 거절하면 실패가 엉뚱한 줄에 붙는다. bind 한 그 줄에서 멈춘다."""
    host = FakeHost(scene_with(enemy_with_hp("full")))

    result = drive(host, ATTACK_AND_COMPARE, "hit", {"enemy": LateSelector(selector="Root[0]/Enemy[7]")})

    assert result.failure.code == COMPARISON_REJECTED
    assert result.failure.payload["declared"] == "float"
    assert result.failure.payload["shape"] == "string"
    # 대입은 첫 statement 다. 게임에는 아무것도 안 나갔다.
    assert host.sent == []


def test_ask_verdict_asks_about_the_step_run_macro_was_called_with() -> None:
    """macro 원문에는 step 이 없다. 판정을 청하는 step 은 그 macro 를 부른 step 이다.

    같은 macro 를 다른 step 에서 부르면 다른 step 을 묻는다. 예전에는 원문에 적힌 번호를
    그대로 냈고, 그 번호는 호출의 step 과 상관이 없었다.
    """
    source = 'def m() -> None:\n    ask_verdict("the board is dealt")\n'

    called_at_three = drive(FakeHost(battle()), source, "m", {}, step=3)
    called_at_seven = drive(FakeHost(battle()), source, "m", {}, step=7)

    assert [one.step for one in called_at_three.verdict_requests] == [3]
    assert [one.step for one in called_at_seven.verdict_requests] == [7]


def test_a_statement_with_no_enclosing_if_still_stands_with_an_empty_observed() -> None:
    host = FakeHost(battle())
    result = drive(
        host, 'def m() -> None:\n    flag("the macro ran")\n', "m", {}
    )

    assert [one.message for one in result.flags] == ["the macro ran"]
    assert result.flags[0].observed == {}


def test_an_if_whose_condition_was_refused_puts_both_branches_in_pending() -> None:
    """거절된 `if` 조건을 거짓으로 읽어 분기를 조용히 넘기면 안 된다.

    조건이 판정되지 않았으므로 어느 쪽도 "원래 안 간다" 가 아니다. 둘 다 `pending` 이고
    `skipped` 는 비어야 한다 — `skipped` 에 넣으면 읽는 쪽이 그 분기가 판정됐다고 읽는다.
    """
    host = FakeHost(
        scene_with(
            card("Root[0]/Go[1]", instance_id=1),
            statics=[
                {
                    "declaring": "Game.X",
                    "member": "y",
                    "value": {"unread": "not-a-number"},
                }
            ],
        )
    )
    source = (
        "def m(card: object) -> None:\n"
        '    if static("X.y") > 5:\n'
        "        click(card)\n"
        "    else:\n"
        "        move_pointer(card)\n"
        '    press_key("Space", 0.1)\n'
    )
    result = drive(
        host, source, "m", {"card": LateSelector(selector="Root[0]/Go[1]")}
    )

    assert result.failure.code == COMPARISON_REJECTED
    assert result.skipped == []
    # 1 `if`, 2 click, 3 move_pointer, 4 press_key. `if` 뒤의 전부가 `pending` 이다.
    assert [place.number for place in result.pending] == [2, 3, 4]
    assert result.applied == []
    assert host.sent == []


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
    host.memory.pulse.held[pressable.key].offers = {"clicks": [{"on": "Button", "method": "Deal"}]}
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
    host.memory.pulse.held[pressable.key].offers = {"clicks": [{"on": "Button", "method": "Deal"}]}
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


# --- runner 가 스스로 멈추는 자리 넷 ---------------------------------------------
#
# 종전에는 `_action` 이 돌려받은 값을 한 번도 안 보고 무조건 `True` 를 돌려줬다. 그래서
# 저자가 `require` 로 미리 상상한 실패만 잡혔고, 클릭이 허공에 떨어져도 사람이 마우스를
# 쥐어도 화면이 안 움직여도 macro 는 끝까지 갔다. 아래가 그 넷을 못박는다.
#
# **안 멈춰야 하는 경우도 같이 잰다.** 멈추는 조건만 재면 아무 때나 멈추는 구현이
# 통과한다.

def held_by_person() -> ActionOutcome:
    return ActionOutcome(
        text="  mouse_down: 전해지지 않음 — 포인터를 사람이 쥐고 있다",
        screen=ScreenChange.still,
        landings=(PressLanding.held_by_person, PressLanding.held_by_person),
    )


def pressed_nothing() -> ActionOutcome:
    return ActionOutcome(
        text="  mouse_down: 닿은 것 없음 — 그 자리에 누를 것이 없다",
        screen=ScreenChange.still,
        landings=(PressLanding.reached_nothing, PressLanding.reached_nothing),
    )


def still_screen() -> ActionOutcome:
    return ActionOutcome(
        text="  mouse_down: ok\n\nNothing on the screen moved.",
        screen=ScreenChange.still,
        landings=(PressLanding.sent, PressLanding.sent),
    )


def clicks(host: FakeHost, count: int = 4):
    card_a = two_cards(host)["card_a"]
    body = "".join("    click(card)\n" for _ in range(count))
    return drive(host, f"def m(card: object) -> None:\n{body}", "m", {"card": card_a})


def test_a_press_that_reached_nothing_stops_the_macro() -> None:
    """설계 때 "click 계열 뒤에 닿았는가를 암묵적 `require` 로 붙인다" 고 한 자리다.

    `REQUIRE_FAILED` 인 이유는 저자가 적었다면 `require(...)` 로 적었을 조건이기
    때문이다. 그 자리에 누를 것이 없었다는 것은 게임이 다르게 동작한 것이다.
    """
    host = FakeHost(battle(), answers=[reached(), pressed_nothing()])
    result = clicks(host)

    assert not result.passed
    assert result.failure.code == REQUIRE_FAILED
    # 두 번째 statement 에서 멈췄으므로 세 번째·네 번째는 안 나갔다.
    assert len(host.sent) == 2
    assert [place.number for place in result.applied] == [1, 2]
    assert [place.number for place in result.pending] == [3, 4]


def test_a_press_that_reached_nothing_but_moved_the_screen_does_not_stop() -> None:
    """`reached` 가 비어도 화면이 움직였으면 게임이 받은 것이다(ARTEL-1174).

    SDK 는 UGUI 버튼이 받은 누름을 `reached` 에 적지 않는다. stage 런 157 에서
    `Canvas/continue` 클릭이 씬을 바꾸고도 여기서 멈췄다.
    """
    moved = ActionOutcome(
        text="  mouse_down: 닿은 것 없음 — 그 자리에 누를 것이 없다",
        screen=ScreenChange.moved,
        landings=(PressLanding.reached_nothing, PressLanding.reached_nothing),
    )
    host = FakeHost(battle(), answers=[moved, moved, moved, moved])
    result = clicks(host)

    assert result.passed, result.failure
    assert len(host.sent) == 4


def test_a_press_that_reached_nothing_with_no_screen_to_read_stops_the_macro() -> None:
    """화면을 모르면(`unknown`) 움직였다는 근거가 없다. 지금처럼 멈춘다."""
    unknown = ActionOutcome(
        text="  mouse_down: 닿은 것 없음 — 그 자리에 누를 것이 없다",
        screen=ScreenChange.unknown,
        landings=(PressLanding.reached_nothing, PressLanding.reached_nothing),
    )
    host = FakeHost(battle(), answers=[unknown])
    result = clicks(host)

    assert not result.passed
    assert result.failure.code == REQUIRE_FAILED
    assert len(host.sent) == 1


def test_a_press_the_person_took_the_mouse_from_stops_the_macro() -> None:
    """`ACTION_REJECTED` 다. 게임도 macro 도 아니라 사람이 마우스를 쥐고 있었다."""
    host = FakeHost(battle(), answers=[held_by_person()])
    result = clicks(host)

    assert not result.passed
    assert result.failure.code == ACTION_REJECTED
    assert "holding the mouse" in result.failure.reason
    assert len(host.sent) == 1


def test_a_batch_where_one_press_still_landed_does_not_stop() -> None:
    """`click` 은 `mouse_down` 과 `mouse_up` 둘을 낸다.

    누르자마자 사라지는 카드를 누르면 뒤엣것이 빈 자리에 떨어진다. 그것까지 실패로 치면
    **성공한 클릭에서 멈춘다.** batch 의 누름이 하나도 못 닿았을 때만 멈춘다.
    """
    half = ActionOutcome(
        text="  mouse_down: Card 에 보냄\n  mouse_up: 닿은 것 없음",
        screen=ScreenChange.moved,
        landings=(PressLanding.sent, PressLanding.reached_nothing),
    )
    host = FakeHost(battle(), answers=[half, half, half, half])
    result = clicks(host)

    assert result.passed, result.failure
    assert len(host.sent) == 4


def test_the_screen_standing_still_once_does_not_stop_the_macro() -> None:
    """한 번은 정상이다. 효과가 1.5초 뒤에 시작하는 animation 일 수 있다."""
    host = FakeHost(battle(), answers=[still_screen()])
    result = clicks(host)

    assert result.passed, result.failure
    assert len(host.sent) == 4


def test_the_screen_standing_still_twice_in_a_row_does_not_stop_the_macro() -> None:
    """둘도 정상이다. 화면을 안 바꾸는 statement 둘이 붙은 macro 를 사람이 평범하게 쓴다."""
    host = FakeHost(battle(), answers=[still_screen(), still_screen()])
    result = clicks(host)

    assert result.passed, result.failure
    assert len(host.sent) == 4


def test_the_screen_standing_still_three_times_in_a_row_stops_the_macro() -> None:
    """셋은 아니다. 안 멈추면 반응 없는 게임에 남은 statement 를 다 쏟아붓는다."""
    assert STILL_SCREENS_BEFORE_STOP == 3
    host = FakeHost(battle(), answers=[still_screen()] * 3)
    result = clicks(host)

    assert not result.passed
    assert result.failure.code == SCREEN_UNCHANGED
    assert len(host.sent) == 3
    assert [place.number for place in result.applied] == [1, 2, 3]
    assert [place.number for place in result.pending] == [4]
    # 멈춘 이유를 payload 가 사람이 읽는 문장으로 말한다.
    assert "3 actions in a row" in result.failure.reason


def test_a_screen_that_moves_in_between_resets_the_count() -> None:
    """**연달아** 다. 움직였다 안 움직였다 하는 macro 는 어디론가 가고 있는 macro 다."""
    host = FakeHost(
        battle(),
        answers=[still_screen(), still_screen(), reached(), still_screen()],
    )
    result = clicks(host)

    assert result.passed, result.failure
    assert len(host.sent) == 4


def test_a_build_that_reports_no_screen_at_all_does_not_stop_the_macro() -> None:
    """`unknown` 은 `still` 이 아니다. 섞으면 화면을 안 보내는 빌드에서 전부 멈춘다."""
    silent = ActionOutcome(
        text="  mouse_down: ok\n\nThe game is not reporting the screen at all.",
        screen=ScreenChange.unknown,
        landings=(PressLanding.sent, PressLanding.sent),
    )
    host = FakeHost(battle(), answers=[silent] * 4)
    result = clicks(host)

    assert result.passed, result.failure
    assert len(host.sent) == 4


def test_the_operator_speaking_stops_the_macro() -> None:
    """무슨 말이든 멈춘다.

    "멈춰" 만 골라내려면 멈추라는 말의 목록이 있어야 하는데 operator 에게 명령어를 알려
    준 적이 없고, 목록에 없는 말로 그만하라고 한 사람은 macro 가 남은 statement 를 다
    쏟아붓는 것을 보게 된다.
    """
    interrupted = ActionOutcome(
        text="  mouse_down: ok",
        screen=ScreenChange.moved,
        landings=(PressLanding.sent,),
        operator_messages=("그만하고 설정 화면 봐줘",),
    )
    host = FakeHost(battle(), answers=[reached(), interrupted])
    result = clicks(host)

    assert not result.passed
    assert result.failure.code == OPERATOR_INTERRUPTED
    assert len(host.sent) == 2
    assert [place.number for place in result.pending] == [3, 4]
    # 그 말이 버려지지 않는다. 멈춘 이유에 그대로 실려 agent 가 읽는다.
    assert "그만하고 설정 화면 봐줘" in result.failure.reason


def test_an_ordinary_macro_with_nobody_speaking_runs_to_the_end() -> None:
    """평범한 성공은 끝까지 간다. 멈추는 조건만 재면 아무 때나 멈추는 구현이 통과한다."""
    host = FakeHost(battle())
    result = drive(host, EXAMPLE, "attack_with_combined_card", two_cards(host))

    assert result.passed, result.failure
    assert len(host.sent) == 4
    assert result.pending == []


def test_a_wedged_game_that_times_out_every_other_batch_still_stops() -> None:
    """**이것이 진짜 멈춘 게임의 궤적이다.**

    `unknown` 은 게임이 결과를 아예 안 줬다는 뜻이고 그것이 `dispatch_actions` 의
    타임아웃이다. 반응 없는 게임은 그 타임아웃을 간헐적으로 내므로 실제로 오는 것은
    `still` 과 `unknown` 이 섞인 줄이다. `unknown` 에서 수를 0 으로 지우면 수가 영영
    3 에 못 닿고, macro 가 죽은 게임에 statement 를 끝까지 다 쏟아붓는다.
    """
    timed_out = ActionOutcome(
        text="The game reported no result.", screen=ScreenChange.unknown
    )
    host = FakeHost(
        battle(),
        answers=[still_screen(), timed_out, still_screen(), timed_out, still_screen()],
    )
    result = clicks(host, count=8)

    assert not result.passed
    assert result.failure.code == SCREEN_UNCHANGED
    # 세 번째 `still` 에서 멈춘다. 그 사이의 `unknown` 둘은 세지도 지우지도 않았다.
    assert len(host.sent) == 5


# --- 반복 (ARTEL-948) -------------------------------------------------------------


def hand_of(*labels: str) -> SceneMemory:
    """손패 카드 여럿과 슬롯 하나. 같은 글자 카드가 둘인 손패를 만들 수 있다."""
    cards = [
        card(f"Root[0]/Hand[2]/Card(Clone)[{index}]", text=label, instance_id=100 + index)
        for index, label in enumerate(labels)
    ]
    return scene_with(*cards, card("Root[0]/Canvas[1]/Slot[9]", instance_id=50))


DEAL_EVERY = '''
def deal(label: string) -> None:
    slot: object = selector("Root[0]/Canvas[1]/Slot[9]")
    for each in find_all(label=label, under="Root[0]/Hand[2]"):
        drag(each, slot)
'''


def test_find_all_walks_both_cards_that_find_would_call_ambiguous() -> None:
    """손패에 `shoot` 이 두 장이다. `find` 는 멈추고 `find_all` 은 둘 다 돈다."""
    host = FakeHost(hand_of("shoot", "slash", "shoot"))

    result = drive(host, DEAL_EVERY, "deal", {"label": "shoot"})

    assert result.passed, result.failure
    assert len(host.sent) == 2
    assert [str(place) for place in result.applied] == [
        "deal step 3 of 3 (pass 1)",
        "deal step 3 of 3 (pass 2)",
    ]


def test_an_empty_find_all_turns_no_passes_and_is_not_a_failure() -> None:
    host = FakeHost(hand_of("slash"))

    result = drive(host, DEAL_EVERY, "deal", {"label": "shoot"})

    assert result.passed, result.failure
    assert host.sent == []
    # 한 회도 안 돈 몸통은 `if` 의 거짓인 가지와 같다 — 원래 안 간다.
    assert [str(place) for place in result.skipped] == ["deal step 3 of 3"]


def test_range_counts_its_passes() -> None:
    host = FakeHost(battle())
    source = 'def m() -> None:\n    for _ in range(3):\n        press_key("Space", 0.1)\n'

    result = drive(host, source, "m", {})

    assert result.passed, result.failure
    assert len(host.sent) == 3
    assert result.executed == 4  # `for` 한 줄과 몸통 세 회


def test_while_reads_its_condition_before_every_pass() -> None:
    """적이 두 번째 타격에 사라진다. 세 번째는 안 나가야 한다."""
    alive = scene_with(enemy_with_hp(10))
    gone = scene_with(card("Root[0]/Other[1]", instance_id=1))

    class EnemyFallsOnTheSecondHit(FakeHost):
        async def run(self, actions, summary: str, step: int) -> ActionOutcome:
            outcome = await super().run(actions, summary, step)
            if len(self.sent) == 2:
                self.memory = gone
            return outcome

    host = EnemyFallsOnTheSecondHit(alive)
    source = (
        "def m(enemy: object) -> None:\n"
        "    while exists(enemy):\n"
        "        click(enemy)\n"
    )

    result = drive(host, source, "m", {"enemy": LateSelector(selector="Root[0]/Enemy[7]")})

    assert result.passed, result.failure
    assert len(host.sent) == 2


def test_a_while_whose_condition_never_turns_false_stops_at_the_pass_limit() -> None:
    """조건이 안 바뀌는 `while` 은 상한에서 멈추고, 왜 멈췄는지 말한다."""
    from app.agents.qa.macro.grammar import MAX_LOOP_PASSES

    host = FakeHost(scene_with(enemy_with_hp(10)))
    source = (
        "def m(enemy: object) -> None:\n"
        "    while exists(enemy):\n"
        '        press_key("Space", 0.1)\n'
    )

    result = drive(host, source, "m", {"enemy": LateSelector(selector="Root[0]/Enemy[7]")})

    assert result.failure.code == LOOP_LIMIT
    assert len(host.sent) == MAX_LOOP_PASSES
    assert "still true" in result.failure.reason
    assert result.failure.payload["observed"] == {"exists(enemy)": "True"}
    # 멈춘 자리는 반복 머리다. 몇 회째에서가 아니라 다음 회에 들어서기 전에 멈췄다.
    assert str(result.stopped_at) == "m step 1 of 2"


def test_a_for_with_more_than_the_limit_to_walk_stops_before_its_first_pass() -> None:
    from app.agents.qa.macro.grammar import MAX_LOOP_PASSES

    host = FakeHost(hand_of(*(["shoot"] * (MAX_LOOP_PASSES + 1))))

    result = drive(host, DEAL_EVERY, "deal", {"label": "shoot"})

    assert result.failure.code == LOOP_LIMIT
    assert host.sent == []


def test_a_range_given_too_many_passes_by_name_stops_at_run_time() -> None:
    """리터럴은 저장 때 막는다. 이름으로 온 수는 실행 때 막는다."""
    host = FakeHost(battle())
    source = 'def m(n: int) -> None:\n    for _ in range(n):\n        press_key("A", 0.1)\n'

    result = drive(host, source, "m", {"n": 51})

    assert result.failure.code == LOOP_LIMIT
    assert host.sent == []


def test_nested_loops_hit_the_executed_statement_limit() -> None:
    """회수 상한만으로는 천장이 안 선다. 중첩이면 곱이 된다."""
    from app.agents.qa.macro.grammar import MAX_EXECUTED_STATEMENTS

    host = FakeHost(battle())
    source = (
        "def m() -> None:\n"
        "    for _ in range(50):\n"
        "        for i in range(50):\n"
        '            press_key("A", 0.1)\n'
    )

    result = drive(host, source, "m", {})

    assert result.failure.code == LOOP_LIMIT
    assert result.executed == MAX_EXECUTED_STATEMENTS + 1
    assert len(host.sent) < MAX_EXECUTED_STATEMENTS


def test_a_failure_inside_a_loop_names_the_pass_it_stopped_on() -> None:
    """두 번째 회의 클릭이 빈 자리에 떨어진다. 몇 회째인지가 payload 에 있어야 한다."""
    empty = ActionOutcome(text="  empty", screen=ScreenChange.still, landings=(PressLanding.reached_nothing,))
    host = FakeHost(battle(), answers=[reached(), empty])
    source = (
        "def m(card: object) -> None:\n"
        "    for _ in range(3):\n"
        "        click(card)\n"
        '    press_key("Space", 0.1)\n'
    )

    result = drive(host, source, "m", {"card": LateSelector(selector="Root[0]/Canvas[1]/Attack[3]")})

    assert result.failure.code == REQUIRE_FAILED
    assert str(result.stopped_at) == "m step 2 of 3 (pass 2)"
    # 적힌 줄을 한 번씩만 든다. 반복의 남은 회는 따로 안 적힌다.
    assert [str(place) for place in result.pending] == ["m step 3 of 3"]


def test_an_if_skipped_on_an_earlier_pass_is_pending_again_on_a_later_one() -> None:
    """지난 회의 `skipped` 가 이번 회의 `pending` 을 가리면, 실패 뒤의 분기가 "원래 안 간다" 로 읽힌다."""
    empty = ActionOutcome(text="  empty", screen=ScreenChange.still, landings=(PressLanding.reached_nothing,))
    host = FakeHost(battle(), answers=[reached(), empty])
    source = (
        "def m(card: object) -> None:\n"
        "    for i in range(2):\n"
        "        click(card)\n"
        "        if i == 0:\n"
        '            press_key("A", 0.1)\n'
        "        else:\n"
        '            press_key("B", 0.1)\n'
    )
    first_pass_press = ActionOutcome(text="  a", screen=ScreenChange.moved, landings=())
    host.answers = [reached(), first_pass_press, empty]

    result = drive(host, source, "m", {"card": LateSelector(selector="Root[0]/Canvas[1]/Attack[3]")})

    assert result.failure.code == REQUIRE_FAILED
    # 둘째 회의 click 에서 멈췄다. 그 회의 `if` 는 판정 전이므로 `if` 자신과 두 가지가 다
    # `pending` 이다. 첫 회에 거짓이던 `else` 가 거기 다시 있는 것이 이 테스트의 요점이다.
    assert [str(place) for place in result.pending] == [
        "m step 3 of 5 (pass 2)",
        "m step 4 of 5 (pass 2)",
        "m step 5 of 5 (pass 2)",
    ]


# --- checkpoint (ARTEL-949) -------------------------------------------------------


CHECKED = '''
def m(card: object) -> None:
    click(card)
    checkpoint("look at the board")
    press_key("Space", 0.1)
'''


def test_a_checkpoint_hands_back_what_ran_so_far_and_what_has_not() -> None:
    host = FakeHost(battle())

    result = drive(host, CHECKED, "m", {"card": LateSelector(selector="Root[0]/Canvas[1]/Attack[3]")})

    assert host.paused == [
        {
            "at": "m step 2 of 3",
            "reason": "look at the board",
            "applied": ["m step 1 of 3"],
            "pending": ["m step 3 of 3"],
            "sent": 1,
        }
    ]
    # 이었으므로 끝까지 갔다.
    assert result.passed, result.failure
    assert len(host.sent) == 2
    assert result.paused_at is None and result.pending == []


def test_stopping_at_a_checkpoint_sends_nothing_after_it_and_is_not_a_failure() -> None:
    host = FakeHost(battle(), decisions=[False])

    result = drive(host, CHECKED, "m", {"card": LateSelector(selector="Root[0]/Canvas[1]/Attack[3]")})

    assert len(host.sent) == 1
    assert result.failure is None
    assert str(result.abandoned_at) == "m step 2 of 3"
    assert not result.passed
    assert [str(place) for place in result.pending] == ["m step 3 of 3"]


def test_resuming_keeps_every_name_the_macro_bound_and_the_loop_it_was_in() -> None:
    """이으면 처음이 아니라 그 줄부터 돈다. bind 한 이름과 반복의 회가 그대로다."""
    host = FakeHost(hand_of("shoot", "shoot"))
    source = (
        "def deal(label: string) -> None:\n"
        '    slot: object = selector("Root[0]/Canvas[1]/Slot[9]")\n'
        '    for each in find_all(label=label, under="Root[0]/Hand[2]"):\n'
        "        checkpoint()\n"
        "        drag(each, slot)\n"
    )

    result = drive(host, source, "deal", {"label": "shoot"})

    assert result.passed, result.failure
    assert [one["at"] for one in host.paused] == [
        "deal step 3 of 4 (pass 1)",
        "deal step 3 of 4 (pass 2)",
    ]
    # 두 번 멈췄고 두 장을 다 냈다. 처음부터 다시 돌았다면 드래그가 넷이다.
    assert len(host.sent) == 2


# --- `wait_until` 과 predicate (ARTEL-988) --------------------------------------


def model_definition(*statements, helpers: tuple[MacroFunction, ...] = ()) -> MacroDefinition:
    """`parser` 가 새 문법을 받기 전에도 `runner` 계약을 직접 검사한다."""
    return MacroDefinition(
        name="m",
        source="model-built macro",
        entry=MacroFunction(name="m", statements=statements),
        helpers=helpers,
    )


def scene_is(name: str) -> MacroComparisonCondition:
    return MacroComparisonCondition(
        left=MacroReaderOperand(call=MacroReaderCall(reader="scene")),
        operator=MacroOperator.equal,
        right=MacroLiteralOperand(literal=MacroLiteral(shape=MacroShape.string, value=name)),
    )


def text_is(selector: str, value: str) -> MacroComparisonCondition:
    return MacroComparisonCondition(
        left=MacroReaderOperand(
            call=MacroReaderCall(reader="text", target=MacroSelectorTarget(selector=selector))
        ),
        operator=MacroOperator.equal,
        right=MacroLiteralOperand(literal=MacroLiteral(shape=MacroShape.string, value=value)),
    )


def wait_for(condition, seconds: float = 30, source: str = "wait_until(ready, 30, remedy)"):
    return MacroWaitUntilStatement(
        source=source, condition=condition, seconds=seconds, remedy="Observe the scene."
    )


class PulseHost(FakeHost):
    def __init__(self, memory: SceneMemory, responses: list[tuple[MacroPulseWait, SceneMemory | None]]):
        super().__init__(memory)
        self.responses = list(responses)
        self.timeouts: list[float] = []

    async def wait_for_pulse(self, timeout: float) -> MacroPulseWait:
        self.timeouts.append(timeout)
        response, memory = self.responses.pop(0)
        if memory is not None:
            self.memory = memory
        return response


def run_model(host: FakeHost, *statements, helpers: tuple[MacroFunction, ...] = ()):
    return asyncio.run(run_macro(host, model_definition(*statements, helpers=helpers), {}, 2))


def test_wait_until_already_true_does_not_ask_host_to_wait() -> None:
    host = PulseHost(scene_with(scene="Ready"), [])
    result = run_model(host, wait_for(scene_is("Ready")))

    assert result.passed
    assert result.waits == ["m step 1 of 1: `wait_until(ready, 30, remedy)` was already true"]
    assert result.waited == 0
    assert host.timeouts == []


def test_wait_until_succeeds_after_two_new_pulses() -> None:
    host = PulseHost(
        scene_with(scene="Loading"),
        [
            (MacroPulseWait(1, 1.2), scene_with(scene="Loading")),
            (MacroPulseWait(1, 2.0), scene_with(scene="Ready")),
        ],
    )
    result = run_model(host, wait_for(scene_is("Ready")))

    assert result.passed
    assert result.waited == pytest.approx(3.2)
    assert result.waits == ["m step 1 of 1: `wait_until(ready, 30, remedy)` was true after 3.2s (2 pulses)"]
    assert host.timeouts == [30, pytest.approx(28.8)]
    assert result.executed == 1


def test_wait_until_statement_timeout_reports_last_evaluation() -> None:
    host = PulseHost(
        scene_with(scene="Loading"),
        [(MacroPulseWait(0, 4.0), None)],
    )
    result = run_model(host, wait_for(scene_is("Ready"), seconds=4))

    assert result.failure.code == WAIT_TIMEOUT
    assert {key: result.failure.payload[key] for key in (
        "expected", "observed", "remedy", "waited_seconds", "readings", "evaluations", "limit"
    )} == {
        "expected": "wait_until(ready, 30, remedy)",
        "observed": {"scene()": "'Loading'"},
        "remedy": "Observe the scene.",
        "waited_seconds": 4.0,
        "readings": 0,
        "evaluations": 1,
        "limit": "statement",
    }
    assert "No pulse arrived, so the screen did not change at all." in result.failure.reason


def test_wait_until_call_budget_exhausted_during_second_statement() -> None:
    host = PulseHost(
        scene_with(scene="Start"),
        [
            (MacroPulseWait(1, 30), scene_with(scene="Middle")),
            (MacroPulseWait(0, 30), None),
        ],
    )
    result = run_model(host, wait_for(scene_is("Middle")), wait_for(scene_is("End")))

    assert result.failure.code == WAIT_TIMEOUT
    assert result.failure.payload["limit"] == "call"
    assert result.failure.payload["waited_seconds"] == 30
    assert result.waited == 60
    assert "the most one call may wait" in result.failure.reason


def test_wait_until_zero_call_budget_does_not_claim_screen_was_unchanged() -> None:
    host = PulseHost(
        scene_with(scene="Start"),
        [
            (MacroPulseWait(1, 30), scene_with(scene="Middle")),
            (MacroPulseWait(1, 30), scene_with(scene="Later")),
        ],
    )
    result = run_model(
        host,
        wait_for(scene_is("Middle")),
        wait_for(scene_is("Later")),
        wait_for(scene_is("End")),
    )

    assert result.failure.code == WAIT_TIMEOUT
    assert result.failure.payload["limit"] == "call"
    assert result.failure.payload["waited_seconds"] == 0
    assert result.failure.payload["evaluations"] == 1
    assert "screen did not change" not in result.failure.reason
    assert host.timeouts == [30, 30]


def test_checkpoint_resume_refills_call_wait_budget() -> None:
    host = PulseHost(
        scene_with(scene="Start"),
        [
            (MacroPulseWait(1, 30), scene_with(scene="Middle")),
            (MacroPulseWait(1, 30), scene_with(scene="Later")),
            (MacroPulseWait(1, 5), scene_with(scene="End")),
        ],
    )
    result = run_model(
        host,
        wait_for(scene_is("Middle")),
        wait_for(scene_is("Later")),
        MacroCheckpointStatement(source="checkpoint()"),
        wait_for(scene_is("End")),
    )

    assert result.passed
    assert result.waited == 65
    assert host.timeouts == [30, 30, 30]


def test_wait_until_treats_direct_selector_not_found_as_false() -> None:
    selector = "Root[0]/Card[1]"
    host = PulseHost(
        scene_with(scene="Battle"),
        [(MacroPulseWait(1, 1), scene_with(card(selector, text="ready")))],
    )
    result = run_model(host, wait_for(text_is(selector, "ready")))

    assert result.passed
    assert result.waited == 1
    assert result.waits[0].endswith("(1 pulses)")


def test_wait_until_treats_predicate_find_not_found_as_false() -> None:
    predicate = MacroFunction(
        name="ready",
        returns="bool",
        statements=(
            MacroAssignStatement(
                source='card: object = find(label="ready")',
                name="card",
                declared_type=MacroType.object_,
                value=MacroFindValue(label=MacroStringLiteral(value="ready")),
            ),
            MacroReturnStatement(source="return True", value=True),
        ),
    )
    host = PulseHost(
        scene_with(scene="Battle"),
        [(MacroPulseWait(1, 1), scene_with(card("Root[0]/Card[1]", text="ready")))],
    )
    result = run_model(host, wait_for(MacroPredicateCondition(callee="ready")), helpers=(predicate,))

    assert result.passed
    assert result.executed == 1


def test_wait_until_selector_ambiguous_stops_immediately() -> None:
    predicate = MacroFunction(
        name="ready",
        returns="bool",
        statements=(
            MacroAssignStatement(
                source='card: object = find(label="ready")',
                name="card",
                declared_type=MacroType.object_,
                value=MacroFindValue(label=MacroStringLiteral(value="ready")),
            ),
            MacroReturnStatement(source="return True", value=True),
        ),
    )
    host = PulseHost(scene_with(card("A", text="ready"), card("B", text="ready")), [])
    result = run_model(host, wait_for(MacroPredicateCondition(callee="ready")), helpers=(predicate,))

    assert result.failure.code == SELECTOR_AMBIGUOUS
    assert result.failure.payload["predicate"] == "ready step 1"
    assert host.timeouts == []


def test_wait_until_operator_message_interrupts_and_keeps_elapsed_time() -> None:
    host = PulseHost(
        scene_with(scene="Loading"),
        [(MacroPulseWait(0, 0.4, ("Stop here",)), None)],
    )
    result = run_model(host, wait_for(scene_is("Ready")))

    assert result.failure.code == OPERATOR_INTERRUPTED
    assert "Stop here" in result.failure.reason
    assert result.waited == pytest.approx(0.4)


def test_wait_until_propagates_qa_cancelled() -> None:
    class CancelledHost(FakeHost):
        async def wait_for_pulse(self, timeout: float) -> MacroPulseWait:
            raise QaCancelled()

    with pytest.raises(QaCancelled):
        run_model(CancelledHost(scene_with(scene="Loading")), wait_for(scene_is("Ready")))


def test_predicate_two_ifs_act_as_or_without_counting_or_skipping_its_steps() -> None:
    predicate = MacroFunction(
        name="one_of_two",
        returns="bool",
        statements=(
            MacroIfStatement(
                source='if scene() == "A":',
                condition=scene_is("A"),
                body=(MacroReturnStatement(source="return True", value=True),),
            ),
            MacroIfStatement(
                source='if scene() == "B":',
                condition=scene_is("B"),
                body=(MacroReturnStatement(source="return True", value=True),),
            ),
            MacroReturnStatement(source="return False", value=False),
        ),
    )
    condition = MacroPredicateCondition(callee="one_of_two")
    result = run_model(
        FakeHost(scene_with(scene="B")),
        MacroRequireStatement(source="require(one_of_two(), remedy)", condition=condition, remedy="Observe."),
        helpers=(predicate,),
    )

    assert result.passed
    assert result.executed == 1
    assert result.skipped == []

    failed = run_model(
        FakeHost(scene_with(scene="C")),
        MacroRequireStatement(source="require(one_of_two(), remedy)", condition=condition, remedy="Observe."),
        helpers=(predicate,),
    )
    assert failed.failure.code == SCENE_MISMATCH
    assert failed.failure.payload["observed"]["one_of_two returned at"] == "'step 5'"
    assert failed.skipped == []


def test_predicate_return_condition_reads_into_callers_observed() -> None:
    predicate = MacroFunction(
        name="ready",
        returns="bool",
        statements=(MacroReturnStatement(source='return scene() == "Ready"', condition=scene_is("Ready")),),
    )
    result = run_model(
        FakeHost(scene_with(scene="Loading")),
        MacroRequireStatement(
            source="require(ready(), remedy)",
            condition=MacroPredicateCondition(callee="ready"),
            remedy="Observe.",
        ),
        helpers=(predicate,),
    )

    assert result.failure.payload["observed"] == {
        "scene()": "'Loading'",
        "ready returned at": "'step 1'",
    }
    assert result.executed == 1


def test_predicate_passes_arguments_to_another_predicate() -> None:
    inner = MacroFunction(
        name="matches_scene",
        returns="bool",
        parameters=(MacroParameter(name="expected", declared_type=MacroType.string),),
        statements=(
            MacroReturnStatement(
                source="return scene() == expected",
                condition=MacroComparisonCondition(
                    left=MacroReaderOperand(call=MacroReaderCall(reader="scene")),
                    operator=MacroOperator.equal,
                    right=MacroNameOperand(name="expected", declared_type=MacroType.string),
                ),
            ),
        ),
    )
    outer = MacroFunction(
        name="ready",
        returns="bool",
        parameters=(MacroParameter(name="expected", declared_type=MacroType.string),),
        statements=(
            MacroReturnStatement(
                source="return matches_scene(expected)",
                condition=MacroPredicateCondition(
                    callee="matches_scene",
                    arguments=(
                        MacroNameArgument(
                            parameter="expected", name="expected", declared_type=MacroType.string
                        ),
                    ),
                ),
            ),
        ),
    )
    result = run_model(
        FakeHost(scene_with(scene="Ready")),
        MacroRequireStatement(
            source="require(ready('Ready'), remedy)",
            condition=MacroPredicateCondition(
                callee="ready",
                arguments=(
                    MacroLiteralArgument(
                        parameter="expected",
                        literal=MacroLiteral(shape=MacroShape.string, value="Ready"),
                    ),
                ),
            ),
            remedy="Observe.",
        ),
        helpers=(outer, inner),
    )

    assert result.passed
    assert result.executed == 1


def test_predicate_failure_in_while_reports_while_line_after_first_pass() -> None:
    predicate = MacroFunction(
        name="card_is_here",
        returns="bool",
        statements=(
            MacroAssignStatement(
                source='card: object = find(label="ready")',
                name="card",
                declared_type=MacroType.object_,
                value=MacroFindValue(label=MacroStringLiteral(value="ready")),
            ),
            MacroReturnStatement(source="return True", value=True),
        ),
    )

    class VanishingCardHost(FakeHost):
        async def checkpoint(self, result) -> bool:
            self.memory = scene_with(scene="Battle")
            return True

    host = VanishingCardHost(scene_with(card("A", text="ready")))
    result = run_model(
        host,
        MacroWhileStatement(
            source="while card_is_here():",
            condition=MacroPredicateCondition(callee="card_is_here"),
            body=(MacroCheckpointStatement(source="checkpoint()"),),
        ),
        helpers=(predicate,),
    )

    assert result.failure.code == SELECTOR_NOT_FOUND
    assert str(result.stopped_at) == "m step 1 of 2"
    assert result.failure.payload["predicate"] == "card_is_here step 1"
    assert result.executed == 2


def test_wait_until_last_not_found_reason_is_in_timeout_observed() -> None:
    selector = "Root[0]/Missing[1]"
    host = PulseHost(scene_with(scene="Battle"), [(MacroPulseWait(0, 1), None)])
    result = run_model(host, wait_for(text_is(selector, "ready"), seconds=1))

    assert result.failure.code == WAIT_TIMEOUT
    assert "matches nothing" in result.failure.payload["observed"]["not found"]
    assert result.failure.payload["evaluations"] == 1


# --- parser 를 거친 source 로 끝까지 ------------------------------------------------
#
# 위 `wait_until` 테스트는 parser 가 새 문법을 받기 전에 쓰여서 모델을 직접 만든다. 아래
# 셋은 저자가 적는 글 그대로를 parser 에 넣고 runner 까지 돌린다 — 두 쪽이 같은 모양을
# 주고받는지는 이 자리에서만 드러난다.

_WAIT_FOR_THE_BANNER = '''
def finish_the_wave() -> None:
    """Wait for the wave to end, then dismiss the banner."""
    wait_until(wave_is_over(), 10, "The wave did not end; observe whether an enemy is left.")
    press_key("Space", 0.1)


def wave_is_over() -> bool:
    if exists(selector("Root[0]/VictoryBanner[0]")):
        return True
    return scene() == "Reward"
'''


def test_a_written_wait_until_with_a_predicate_waits_for_the_second_alternative() -> None:
    # 배너는 끝내 안 뜨고 scene 이 바로 넘어간다. `or` 의 두 번째 가지가 참이 되는 경우다.
    host = PulseHost(
        scene_with(scene="Battle"),
        [(MacroPulseWait(1, 1.5), scene_with(scene="Reward"))],
    )
    result = drive(host, _WAIT_FOR_THE_BANNER, "finish_the_wave", {})

    assert result.passed
    assert result.waits == [
        'finish_the_wave step 1 of 2: `wait_until(wave_is_over(), 10, "The wave did not end; observe whether an enemy '
        'is left.")` was true after 1.5s (1 pulses)'
    ]
    assert len(result.applied) == 1


def test_a_written_wait_until_that_times_out_names_the_return_it_ended_on() -> None:
    host = PulseHost(
        scene_with(scene="Battle"),
        [(MacroPulseWait(1, 4.0), None), (MacroPulseWait(0, 6.0), None)],
    )
    result = drive(host, _WAIT_FOR_THE_BANNER, "finish_the_wave", {})

    assert result.failure.code == WAIT_TIMEOUT
    assert result.failure.payload["readings"] == 1
    assert result.failure.payload["evaluations"] == 2
    assert result.failure.payload["observed"]["wave_is_over returned at"] == "'step 3'"
    # 기다리다 멈춘 자리 뒤의 `press_key` 는 나가지 않았다.
    assert result.applied == []
    assert [str(place) for place in result.pending] == ["finish_the_wave step 2 of 2"]


def test_a_written_wait_until_counts_a_find_that_matches_nothing_as_not_yet() -> None:
    source = '''
def open_the_reward() -> None:
    """Wait for the reward chest to appear, then open it."""
    wait_until(chest_is_there(), 5, "No chest appeared; observe the reward screen.")
    chest: object = find(name="Chest")
    click(chest)


def chest_is_there() -> bool:
    chest: object = find(name="Chest")
    return exists(chest)
'''
    host = PulseHost(
        scene_with(scene="Reward"),
        [(MacroPulseWait(1, 0.8), scene_with(card("Root[0]/Chest[0]", instance_id=7), scene="Reward"))],
    )
    result = drive(host, source, "open_the_reward", {})

    assert result.waits == [
        'open_the_reward step 1 of 3: `wait_until(chest_is_there(), 5, "No chest appeared; observe the reward screen.")` '
        "was true after 0.8s (1 pulses)"
    ]
    assert len(result.applied) == 1


# --- `import`, `match decide(...)`, `ask(...)` --------------------------------------
#
# parser 가 아직 새 문법을 안 받으므로 정의를 model 로 직접 세운다.

from app.agents.qa.macro.errors import JEV_FAILED  # noqa: E402
from app.agents.qa.macro.grammar import NONE_FITS, NONE_FITS_DESCRIPTION  # noqa: E402
from app.agents.qa.macro.model import (  # noqa: E402
    MacroActionStatement,
    MacroAskCondition,
    MacroContextLiteral,
    MacroDecideCall,
    MacroForStatement,
    MacroHelperCallStatement,
    MacroMatchCase,
    MacroMatchStatement,
    MacroRangeIterable,
)
from app.agents.qa.macro.runner import MacroJevUnavailable  # noqa: E402


def press(key: str) -> MacroActionStatement:
    """`press_key` 하나. 어느 몸통이 돌았는지를 `summaries` 로 읽으려고 key 를 이름처럼 쓴다."""
    return MacroActionStatement(
        source=f'press_key("{key}", 0.1)',
        callee="press_key",
        arguments=(
            MacroLiteralArgument(
                parameter="key_code", literal=MacroLiteral(shape=MacroShape.string, value=key)
            ),
            MacroLiteralArgument(
                parameter="duration_seconds",
                literal=MacroLiteral(shape=MacroShape.number, value=0.1),
            ),
        ),
    )


def call(callee: str) -> MacroHelperCallStatement:
    return MacroHelperCallStatement(source=f"{callee}()", callee=callee)


def function(name: str, *statements, docstring: str | None = None) -> MacroFunction:
    return MacroFunction(name=name, docstring=docstring, statements=statements)


def macro(
    name: str,
    *statements,
    docstring: str | None = None,
    helpers: tuple[MacroFunction, ...] = (),
    imports: tuple[MacroDefinition, ...] = (),
) -> MacroDefinition:
    return MacroDefinition(
        name=name,
        source="model-built macro",
        entry=function(name, *statements, docstring=docstring),
        helpers=helpers,
        imports=imports,
    )


def match(*options: tuple[str, tuple], question: str = "What now?", context=()) -> MacroMatchStatement:
    return MacroMatchStatement(
        source=f'match decide("{question}"):',
        decide=MacroDecideCall(question=question, context=context),
        cases=tuple(MacroMatchCase(option=option, body=body) for option, body in options),
    )


def ask(question: str = "Is it on?") -> MacroAskCondition:
    return MacroAskCondition(question=question)


class JevHost(FakeHost):
    """`decide` 와 `ask` 가 적어 둔 답을 차례로 낸다. 예외를 적어 두면 그것을 낸다."""

    def __init__(
        self,
        memory: SceneMemory | None = None,
        picks: list | None = None,
        probabilities: list | None = None,
        decisions: list[bool] | None = None,
    ) -> None:
        super().__init__(memory or scene_with(scene="Battle"), decisions=decisions)
        self.picks = list(picks or [])
        self.probabilities = list(probabilities or [])
        self.decide_requests: list = []
        self.ask_requests: list = []
        self.paused_decisions: list = []

    async def decide(self, request) -> str:
        self.decide_requests.append(request)
        answer = self.picks.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def ask(self, request) -> float:
        self.ask_requests.append(request)
        answer = self.probabilities.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def checkpoint(self, result) -> bool:
        self.paused_decisions.append(result.paused_decision)
        return await super().checkpoint(result)

    def keys(self) -> list[str]:
        return [actions[0].params[0] for actions, _, _ in self.sent]


def run_definition(host: FakeHost, definition: MacroDefinition):
    return asyncio.run(run_macro(host, definition, {}, 2))


def fight() -> MacroDefinition:
    return macro(
        "fight",
        call("strike"),
        docstring="Strike the boss once.",
        helpers=(function("strike", press("F")),),
    )


def heal() -> MacroDefinition:
    return macro("heal", press("H"), docstring="Heal the player with the holy card.")


def test_an_imported_macro_runs_its_own_helper_not_the_callers_helper_of_the_same_name() -> None:
    host = JevHost()
    root = macro(
        "m",
        call("fight"),
        call("strike"),
        helpers=(function("strike", press("R")),),
        imports=(fight(),),
    )

    result = run_definition(host, root)

    assert result.passed, result.failure
    assert host.keys() == ["F", "R"]
    assert [str(place) for place in result.applied] == [
        "strike step 1 of 1",
        "strike step 1 of 1",
    ]


def test_an_imported_macro_resolves_its_own_imports() -> None:
    host = JevHost()
    inner = macro("inner", press("I"), docstring="Inner.")
    middle = macro("middle", call("inner"), press("M"), docstring="Middle.", imports=(inner,))
    root = macro("m", call("middle"), press("R"), imports=(middle,))

    result = run_definition(host, root)

    assert result.passed, result.failure
    assert host.keys() == ["I", "M", "R"]
    assert [str(place) for place in result.applied] == [
        "inner step 1 of 1",
        "middle step 2 of 2",
        "m step 2 of 2",
    ]


def test_a_failure_inside_an_imported_macro_names_the_import_in_the_chain() -> None:
    host = JevHost()
    broken = macro(
        "broken",
        MacroRequireStatement(
            source='require(scene() == "Elsewhere", "Go elsewhere.")',
            condition=scene_is("Elsewhere"),
            remedy="Go elsewhere.",
        ),
        docstring="Broken.",
    )
    root = macro("m", call("broken"), press("R"), imports=(broken,))

    result = run_definition(host, root)

    assert result.failure.code == SCENE_MISMATCH
    assert [str(place) for place in result.chain] == ["m step 1 of 2", "broken step 1 of 1"]
    assert [str(place) for place in result.pending] == ["m step 2 of 2"]
    assert result.failure.payload["macro"] == "m"


def test_a_checkpoint_inside_an_imported_macro_pauses_the_whole_run() -> None:
    host = JevHost(decisions=[False])
    paused = macro(
        "paused",
        MacroCheckpointStatement(source='checkpoint("look")', reason="look"),
        press("P"),
        docstring="Pauses.",
    )
    root = macro("m", call("paused"), press("R"), imports=(paused,))

    result = run_definition(host, root)

    assert host.paused[0]["at"] == "paused step 1 of 2"
    assert host.paused[0]["pending"] == ["paused step 2 of 2", "m step 2 of 2"]
    assert host.paused_decisions == [None]
    assert host.sent == []
    assert str(result.abandoned_at) == "paused step 1 of 2"
    assert result.failure is None


def fight_or_heal(**keywords) -> MacroDefinition:
    return macro(
        "m",
        match(("fight", (call("fight"),)), ("heal", (call("heal"), press("X"))), **keywords),
        press("Z"),
        imports=(fight(), heal()),
    )


@pytest.mark.parametrize(
    ("pick", "keys", "skipped"),
    [
        ("fight", ["F", "Z"], ["m step 3 of 5", "m step 4 of 5"]),
        ("heal", ["H", "X", "Z"], ["m step 2 of 5"]),
    ],
)
def test_decide_runs_only_the_case_jev_picked_and_skips_the_others(pick, keys, skipped) -> None:
    host = JevHost(picks=[pick])

    result = run_definition(host, fight_or_heal())

    assert result.passed, result.failure
    assert host.keys() == keys
    assert [str(place) for place in result.skipped] == skipped
    assert result.decisions == [f"m step 1 of 5: decide picked {pick}"]
    # `match` 하나와 고른 몸통만 센다. fight 는 `match`·`fight()`·`strike()`·F·Z, heal 은
    # `match`·`heal()`·H·X·Z 로 둘 다 다섯이다.
    assert result.executed == 5


def test_the_options_are_the_imported_docstrings_with_none_fits_last() -> None:
    goal = MacroContextLiteral(
        key="goal", literal=MacroLiteral(shape=MacroShape.string, value="beat the boss")
    )
    host = JevHost(picks=["fight"])

    run_definition(host, fight_or_heal(question="Which?", context=(goal,)))

    (request,) = host.decide_requests
    assert request.question == "Which?"
    assert [(option.name, option.description) for option in request.options] == [
        ("fight", "Strike the boss once."),
        ("heal", "Heal the player with the holy card."),
        (NONE_FITS, NONE_FITS_DESCRIPTION),
    ]
    assert request.state == {"scene": "Battle", "goal": "beat the boss"}
    assert request.clipped == 0


def test_a_case_that_names_no_imported_macro_has_an_empty_description() -> None:
    host = JevHost(picks=["wait"])
    root = macro(
        "m",
        match(("fight", (call("fight"),)), ("wait", (press("W"),))),
        imports=(fight(),),
    )

    result = run_definition(host, root)

    assert result.passed, result.failure
    assert host.keys() == ["W"]
    assert host.decide_requests[0].options[1].description == ""


def test_none_fits_pauses_with_the_decision_and_resuming_goes_on_after_the_match() -> None:
    host = JevHost(picks=[NONE_FITS], decisions=[True])

    result = run_definition(host, fight_or_heal(question="Which?"))

    assert result.passed, result.failure
    assert host.keys() == ["Z"]
    (decision,) = host.paused_decisions
    assert decision is host.decide_requests[0]
    assert host.paused == [
        {
            "at": "m step 1 of 5",
            "reason": "no imported macro fits — Which?",
            "applied": [],
            "pending": ["m step 5 of 5"],
            "sent": 0,
        }
    ]
    assert result.decisions == [f"m step 1 of 5: decide picked {NONE_FITS}"]
    assert [str(place) for place in result.skipped] == [
        "m step 2 of 5",
        "m step 3 of 5",
        "m step 4 of 5",
    ]
    assert result.paused_decision is None and result.paused_at is None


def test_none_fits_and_stopping_abandons_the_macro() -> None:
    host = JevHost(picks=[NONE_FITS], decisions=[False])

    result = run_definition(host, fight_or_heal())

    assert host.sent == []
    assert result.failure is None
    assert str(result.abandoned_at) == "m step 1 of 5"
    assert not result.passed
    assert result.paused_decision is None


def test_decide_inside_a_loop_asks_once_per_pass_and_names_the_pass() -> None:
    host = JevHost(picks=["fight", "heal", "fight"])
    loop = MacroForStatement(
        source="for turn in range(3):",
        name="turn",
        declared_type=MacroType.int_,
        iterable=MacroRangeIterable(count=MacroLiteral(shape=MacroShape.number, value=3)),
        body=(match(("fight", (call("fight"),)), ("heal", (call("heal"),))),),
    )
    root = macro("m", loop, imports=(fight(), heal()))

    result = run_definition(host, root)

    assert result.passed, result.failure
    assert host.keys() == ["F", "H", "F"]
    assert result.decisions == [
        "m step 2 of 4 (pass 1): decide picked fight",
        "m step 2 of 4 (pass 2): decide picked heal",
        "m step 2 of 4 (pass 3): decide picked fight",
    ]
    assert len(host.decide_requests) == 3


def test_an_answer_that_is_not_an_option_stops_with_jev_failed() -> None:
    host = JevHost(picks=["flee"])

    result = run_definition(host, fight_or_heal(question="Which?"))

    assert result.failure.code == JEV_FAILED
    assert result.failure.payload["question"] == "Which?"
    assert "'flee'" in result.failure.payload["reason"]
    assert str(result.stopped_at) == "m step 1 of 5"
    assert host.sent == []
    assert result.decisions == []


def test_jev_unavailable_on_decide_stops_with_jev_failed() -> None:
    host = JevHost(picks=[MacroJevUnavailable("HTTP 503")])

    result = run_definition(host, fight_or_heal(question="Which?"))

    assert result.failure.code == JEV_FAILED
    assert result.failure.payload["question"] == "Which?"
    assert result.failure.payload["reason"] == "HTTP 503"
    assert [str(place) for place in result.pending] == [
        "m step 2 of 5",
        "m step 3 of 5",
        "m step 4 of 5",
        "m step 5 of 5",
    ]


def test_a_decide_that_leaves_objects_out_says_how_many() -> None:
    crowd = scene_with(
        *(card(f"Root[0]/Crowd[1]/Member[{index}]", text="x" * 70) for index in range(200))
    )
    host = JevHost(crowd, picks=["fight"])

    result = run_definition(host, fight_or_heal())

    clipped = host.decide_requests[0].clipped
    assert clipped > 0
    assert result.decisions == [
        f"m step 1 of 5: decide picked fight ({clipped} objects left out of the context)"
    ]


@pytest.mark.parametrize(("probability", "keys"), [(0.5, ["Y"]), (0.49, ["N"])])
def test_ask_in_an_if_is_true_from_one_half(probability, keys) -> None:
    host = JevHost(probabilities=[probability])
    branch = MacroIfStatement(
        source='if ask("Is it on?"):', condition=ask(), body=(press("Y"),), orelse=(press("N"),)
    )

    result = run_definition(host, macro("m", branch))

    assert result.passed, result.failure
    assert host.keys() == keys
    value = "true" if probability >= 0.5 else "false"
    assert result.decisions == [f"m step 1 of 3: ask 'Is it on?' -> {probability:.2f} ({value})"]
    assert host.ask_requests[0].state == {"scene": "Battle", "objects": []}


def test_ask_in_an_elif_is_asked_only_when_the_if_was_false() -> None:
    host = JevHost(probabilities=[0.2, 0.8])
    inner = MacroIfStatement(
        source='elif ask("Second?"):', condition=ask("Second?"), body=(press("B"),)
    )
    branch = MacroIfStatement(
        source='if ask("First?"):', condition=ask("First?"), body=(press("A"),), orelse=(inner,)
    )

    result = run_definition(host, macro("m", branch))

    assert host.keys() == ["B"]
    assert [request.question for request in host.ask_requests] == ["First?", "Second?"]
    assert result.decisions == [
        "m step 1 of 4: ask 'First?' -> 0.20 (false)",
        "m step 3 of 4: ask 'Second?' -> 0.80 (true)",
    ]
    assert [str(place) for place in result.skipped] == ["m step 2 of 4"]


def test_ask_as_a_while_guard_is_asked_before_every_pass() -> None:
    host = JevHost(probabilities=[0.9, 0.5, 0.1])
    loop = MacroWhileStatement(
        source='while ask("Still fighting?"):', condition=ask("Still fighting?"), body=(press("F"),)
    )

    result = run_definition(host, macro("m", loop, press("Z")))

    assert result.passed, result.failure
    assert host.keys() == ["F", "F", "Z"]
    assert result.decisions == [
        "m step 1 of 3: ask 'Still fighting?' -> 0.90 (true)",
        "m step 1 of 3: ask 'Still fighting?' -> 0.50 (true)",
        "m step 1 of 3: ask 'Still fighting?' -> 0.10 (false)",
    ]


def test_ask_in_a_require_that_is_false_carries_the_probability() -> None:
    host = JevHost(probabilities=[0.49])
    requirement = MacroRequireStatement(
        source='require(ask("Is the boss down?"), "Hit it again.")',
        condition=ask("Is the boss down?"),
        remedy="Hit it again.",
    )

    result = run_definition(host, macro("m", requirement, press("Z")))

    assert result.failure.code == SCENE_MISMATCH
    assert result.failure.payload["observed"] == {
        "ask": repr({"question": "Is the boss down?", "probability": 0.49})
    }
    assert host.sent == []


def test_ask_in_a_require_that_is_true_goes_on() -> None:
    host = JevHost(probabilities=[0.5])
    requirement = MacroRequireStatement(
        source='require(ask("Ready?"), "Wait.")', condition=ask("Ready?"), remedy="Wait."
    )

    result = run_definition(host, macro("m", requirement, press("Z")))

    assert result.passed, result.failure
    assert host.keys() == ["Z"]


def test_jev_unavailable_on_ask_stops_with_jev_failed() -> None:
    host = JevHost(probabilities=[MacroJevUnavailable("no API key")])
    loop = MacroWhileStatement(
        source='while ask("Still fighting?"):', condition=ask("Still fighting?"), body=(press("F"),)
    )

    result = run_definition(host, macro("m", loop))

    assert result.failure.code == JEV_FAILED
    assert result.failure.payload["question"] == "Still fighting?"
    assert result.failure.payload["reason"] == "no API key"
    assert str(result.stopped_at) == "m step 1 of 2"
    assert host.sent == []


# --- 숫자 `def`·산술식·`at_start` (ARTEL-1066) ------------------------------------
#
# planner–executer 엔진의 executer 가 `progress`·`risk`·`setbacks` 를 매 턴 부른다. 그 셋을
# 본뜬 step module 하나로 조건 자리 넷과 `evaluate_number` 를 잰다.

import copy  # noqa: E402

from app.agents.qa.macro.errors import (  # noqa: E402
    ARITHMETIC_REJECTED,
    AT_START_UNAVAILABLE,
    DIVIDED_BY_ZERO,
    MacroFailure,
)
from app.agents.qa.macro.runner import StepStartValues, evaluate_number  # noqa: E402

PROGRESS_MODULE = '''
def step() -> None:
    if progress() >= 0.5:
        flag("half way")
    require(setbacks() == 0, "The player lost a life; replan.")

def progress() -> float:
    enemy: object = find(name="Enemy")
    hp: float = member(enemy, "Enemy.Hp")
    start: float = at_start(enemy, "Enemy.Hp")
    if start <= 0:
        return 1
    return max(0, min(1, (start - hp) / start))

def risk(weight: int = 1) -> float:
    return weight * (1 - progress())

def setbacks() -> int:
    lost: int = at_start("Game.Lives") - static("Game.Lives")
    return max(lost, 0)

def ratio(a: float, b: float) -> float:
    return a / b

def plain(weight: float = 2.5) -> float:
    return -weight + 2 * 3 - max(1, 2, 3)
'''


def fight_scene(hp, lives) -> SceneMemory:
    return scene_with(
        enemy_with_hp(hp),
        statics=[{"declaring": "Game.Game", "member": "Lives", "value": lives}],
    )


class StepStartHost(FakeHost):
    """step 이 시작될 때의 `pulse` 를 들고 있는 host. planner–executer 엔진이 이 모양이다."""

    def __init__(self, memory: SceneMemory, at_start: SceneMemory | None) -> None:
        super().__init__(memory)
        self.at_start = None if at_start is None else copy.deepcopy(at_start.pulse)

    def pulse_at_step_start(self):
        return self.at_start


def progress_module() -> MacroDefinition:
    return macro_definition_from_source("step", PROGRESS_MODULE)


def test_a_host_that_keeps_step_start_values_is_a_step_start_values() -> None:
    assert isinstance(StepStartHost(fight_scene(40, 3), fight_scene(80, 3)), StepStartValues)
    assert not isinstance(FakeHost(fight_scene(40, 3)), StepStartValues)


def test_evaluate_number_returns_progress_from_the_step_start_value() -> None:
    host = StepStartHost(fight_scene(20, 3), at_start=fight_scene(80, 3))

    assert evaluate_number(host, progress_module(), "progress") == 0.75
    assert evaluate_number(host, progress_module(), "setbacks") == 0


def test_evaluate_number_passes_arguments_and_nested_number_defs() -> None:
    host = StepStartHost(fight_scene(60, 3), at_start=fight_scene(80, 3))

    assert evaluate_number(host, progress_module(), "risk") == 0.75
    assert evaluate_number(host, progress_module(), "risk", {"weight": 2}) == 1.5
    assert evaluate_number(host, progress_module(), "ratio", {"a": 3, "b": 4}) == 0.75


def test_evaluate_number_does_unary_minus_and_precedence_like_python() -> None:
    host = FakeHost(fight_scene(1, 1))

    assert evaluate_number(host, progress_module(), "plain") == -2.5 + 6 - 3
    assert evaluate_number(host, progress_module(), "plain", {"weight": 1}) == 2


def test_min_and_max_clamp_the_progress() -> None:
    # 시작보다 hp 가 많아지면 0 아래로, 0 이 되면 1 위로 가지 않는다.
    healed = StepStartHost(fight_scene(100, 3), at_start=fight_scene(80, 3))
    dead = StepStartHost(fight_scene(-10, 3), at_start=fight_scene(80, 3))

    assert evaluate_number(healed, progress_module(), "progress") == 0
    assert evaluate_number(dead, progress_module(), "progress") == 1


def test_setbacks_counts_lives_lost_since_the_step_began() -> None:
    host = StepStartHost(fight_scene(80, 1), at_start=fight_scene(80, 3))

    assert evaluate_number(host, progress_module(), "setbacks") == 2
    assert isinstance(evaluate_number(host, progress_module(), "setbacks"), int)


def test_evaluate_number_refuses_a_name_that_is_not_a_number_def() -> None:
    host = FakeHost(fight_scene(1, 1))

    with pytest.raises(ValueError):
        evaluate_number(host, progress_module(), "step")
    with pytest.raises(ValueError):
        evaluate_number(host, progress_module(), "ratio", {"a": 1})


def test_division_by_zero_stops_with_its_own_code() -> None:
    host = FakeHost(fight_scene(1, 1))

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, progress_module(), "ratio", {"a": 1, "b": 0})

    assert failed.value.code == DIVIDED_BY_ZERO
    assert failed.value.payload["number_def"] == "ratio step 1"


def test_at_start_without_host_support_stops_with_its_own_code() -> None:
    host = FakeHost(fight_scene(20, 3))

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, progress_module(), "progress")

    assert failed.value.code == AT_START_UNAVAILABLE
    assert "keeps no such values" in failed.value.reason
    assert failed.value.payload["number_def"] == "progress step 3"


def test_at_start_with_no_step_begun_yet_stops_with_the_same_code() -> None:
    host = StepStartHost(fight_scene(20, 3), at_start=None)

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, progress_module(), "setbacks")

    assert failed.value.code == AT_START_UNAVAILABLE


def test_at_start_of_an_object_missing_at_step_start_stops() -> None:
    host = StepStartHost(fight_scene(20, 3), at_start=scene_with(statics=[]))

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, progress_module(), "progress")

    assert failed.value.code == AT_START_UNAVAILABLE
    assert "was not in the pulse when this step began" in failed.value.reason


def test_at_start_of_a_member_missing_at_step_start_names_what_was_there() -> None:
    host = StepStartHost(fight_scene(20, 3), at_start=scene_with(statics=[]))

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, progress_module(), "setbacks")

    assert failed.value.code == AT_START_UNAVAILABLE
    assert failed.value.payload["available"] == []


def test_at_start_reads_an_object_that_is_gone_now() -> None:
    """step 이 시작될 때 있던 object 는 지금 사라졌어도 그때의 값을 읽는다."""
    source = (
        "def m() -> None:\n"
        '    require(at_start(selector("Root[0]/Enemy[7]"), "Enemy.Hp") == 80, "x")\n'
    )
    host = StepStartHost(scene_with(), at_start=fight_scene(80, 3))

    assert drive(host, source, "m", {}).passed


def test_a_reader_that_arrives_as_text_is_refused_in_arithmetic() -> None:
    source = (
        "def m() -> None:\n"
        '    require(p() > 0, "x")\n\n'
        "def p() -> float:\n"
        '    return static("Game.Name") * 2\n'
    )
    host = FakeHost(
        scene_with(statics=[{"declaring": "Game.Game", "member": "Name", "value": "hero"}])
    )
    result = drive(host, source, "m", {})

    assert result.failure.code == ARITHMETIC_REJECTED
    assert result.failure.payload["number_def"] == "p step 1"


def test_a_number_def_returning_a_reader_of_another_shape_is_refused() -> None:
    source = (
        "def m() -> None:\n"
        '    require(p() > 0, "x")\n\n'
        "def p() -> float:\n"
        '    return static("Game.Name")\n'
    )
    host = FakeHost(
        scene_with(statics=[{"declaring": "Game.Game", "member": "Name", "value": "hero"}])
    )
    result = drive(host, source, "m", {})

    assert result.failure.code == ARITHMETIC_REJECTED


@pytest.mark.parametrize(
    ("hp", "flagged"),
    [(20, True), (60, False)],
    ids=["past half", "before half"],
)
def test_a_number_def_drives_an_if(hp: int, flagged: bool) -> None:
    host = StepStartHost(fight_scene(hp, 3), at_start=fight_scene(80, 3))
    result = asyncio.run(run_macro(host, progress_module(), {}, 2))

    assert result.passed
    assert bool(result.flags) is flagged


def test_a_number_def_drives_a_require_and_reports_what_it_returned() -> None:
    host = StepStartHost(fight_scene(80, 2), at_start=fight_scene(80, 3))
    result = asyncio.run(run_macro(host, progress_module(), {}, 2))

    assert result.failure.code == SCENE_MISMATCH
    assert result.failure.payload["observed"]["setbacks()"] == "1"
    assert result.failure.payload["observed"]["setbacks returned at"] == "'step 2'"


def test_a_number_def_drives_a_while() -> None:
    """한 회마다 hp 가 20 씩 준다. `progress` 가 0.5 에 닿으면 반복이 끝난다."""

    class Hits(StepStartHost):
        async def run(self, actions, summary: str, step: int) -> ActionOutcome:
            outcome = await super().run(actions, summary, step)
            members = self.memory.pulse.held["Battle/Root[0]/Enemy[7]"].members
            hp = next(iter(members.values())).value
            self.memory = fight_scene(hp - 20, 3)
            return outcome

    source = PROGRESS_MODULE.replace(
        '    if progress() >= 0.5:\n        flag("half way")\n',
        '    while progress() < 0.5:\n        press_key("Space", 0.1)\n',
    )
    host = Hits(fight_scene(80, 3), at_start=fight_scene(80, 3))
    result = drive(host, source, "step", {})

    assert result.passed
    assert len(host.sent) == 2


def test_a_number_def_drives_a_wait_until() -> None:
    class StepStartPulseHost(PulseHost):
        def pulse_at_step_start(self):
            return copy.deepcopy(fight_scene(80, 3).pulse)

    host = StepStartPulseHost(
        fight_scene(80, 3), [(MacroPulseWait(1, 0.5), fight_scene(30, 3))]
    )
    source = PROGRESS_MODULE.replace(
        '    if progress() >= 0.5:\n        flag("half way")\n',
        '    wait_until(progress() >= 0.5, 5, "The enemy did not lose half its hp.")\n',
    )
    result = drive(host, source, "step", {})

    assert result.passed
    assert "was true after 0.5s (1 pulses)" in result.waits[0]


# 산술과 `at_start` 없이 도는 숫자 `def`(ARTEL-1066). `evaluate_number` 와 조건 자리를 잰다.

NUMBER_MODULE = '''
def step() -> None:
    if hp() <= 40:
        flag("half way")
    while hp() > 0:
        press_key("Space", 0.1)
    require(lives() == 3, "The player lost a life; replan.")

def hp() -> float:
    enemy: object = find(name="Enemy")
    return member(enemy, "Enemy.Hp")

def remaining() -> float:
    return hp()

def lives() -> int:
    return static("Game.Lives")

def threshold(limit: float = 40) -> float:
    return limit

def scaled(value: float) -> float:
    return value
'''


def number_module() -> MacroDefinition:
    return macro_definition_from_source("step", NUMBER_MODULE)


class LosingHp(FakeHost):
    """action 이 하나 나갈 때마다 적의 hp 가 20 준다."""

    def __init__(self, hp: int, lives: int) -> None:
        super().__init__(fight_scene(hp, lives))
        self.hp = hp
        self.lives = lives

    async def run(self, actions, summary: str, step: int) -> ActionOutcome:
        outcome = await super().run(actions, summary, step)
        self.hp -= 20
        self.memory = fight_scene(self.hp, self.lives)
        return outcome


def test_evaluate_number_returns_what_a_number_def_reads() -> None:
    host = FakeHost(fight_scene(30, 3))

    assert evaluate_number(host, number_module(), "hp") == 30
    assert evaluate_number(host, number_module(), "lives") == 3
    assert isinstance(evaluate_number(host, number_module(), "lives"), int)


def test_evaluate_number_takes_arguments_defaults_and_nested_calls() -> None:
    host = FakeHost(fight_scene(30, 3))

    assert evaluate_number(host, number_module(), "threshold") == 40
    assert evaluate_number(host, number_module(), "threshold", {"limit": 7.5}) == 7.5
    assert evaluate_number(host, number_module(), "remaining") == 30


def test_evaluate_number_refuses_the_entry_point_and_a_missing_argument() -> None:
    host = FakeHost(fight_scene(30, 3))

    with pytest.raises(ValueError):
        evaluate_number(host, number_module(), "step")
    with pytest.raises(ValueError):
        evaluate_number(host, number_module(), "scaled")


def test_a_number_def_that_fails_names_its_own_step() -> None:
    host = FakeHost(scene_with(statics=[]))

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, number_module(), "remaining")

    assert failed.value.code == SELECTOR_NOT_FOUND
    assert failed.value.payload["number_def"] == "hp step 1"


def test_a_number_def_drives_if_while_and_require() -> None:
    host = LosingHp(hp=60, lives=3)
    result = asyncio.run(run_macro(host, number_module(), {}, 2))

    assert result.passed
    # 60 → 40 → 20 → 0. `if` 는 60 에서 판정해 `flag` 를 건너뛴다.
    assert len(host.sent) == 3
    assert result.flags == []


def test_a_number_def_in_a_failed_require_reports_what_it_returned() -> None:
    host = LosingHp(hp=0, lives=2)
    result = asyncio.run(run_macro(host, number_module(), {}, 2))

    assert result.failure.code == SCENE_MISMATCH
    assert [one.message for one in result.flags] == ["half way"]
    assert result.failure.payload["observed"]["lives()"] == "2"
    assert result.failure.payload["observed"]["lives returned at"] == "'step 1'"


def test_a_number_def_without_arithmetic_drives_a_wait_until() -> None:
    host = PulseHost(fight_scene(80, 3), [(MacroPulseWait(1, 0.5), fight_scene(30, 3))])
    source = NUMBER_MODULE.replace(
        '    if hp() <= 40:\n        flag("half way")\n',
        '    wait_until(hp() <= 40, 5, "The enemy did not lose half its hp.")\n',
    ).replace('    while hp() > 0:\n        press_key("Space", 0.1)\n', "")
    result = drive(host, source, "step", {})

    assert result.passed
    assert "was true after 0.5s (1 pulses)" in result.waits[0]


# 산술식(ARTEL-1076). `at_start` 없이 도는 식으로 잰다.

ARITHMETIC_MODULE = '''
def m() -> None:
    require(lost() >= 0.5, "The enemy still has more than half its hp.")

def lost() -> float:
    return 1 - hp() / 80

def hp() -> float:
    enemy: object = find(name="Enemy")
    return member(enemy, "Enemy.Hp")

def ratio(a: float, b: float) -> float:
    return a / b

def plain(weight: float = 2.5) -> float:
    return -weight + 2 * 3 - max(1, 2, 3)

def clamped(value: float) -> float:
    return max(0, min(1, value))
'''


def arithmetic_module() -> MacroDefinition:
    return macro_definition_from_source("m", ARITHMETIC_MODULE)


def test_arithmetic_follows_python_precedence_and_unary_minus() -> None:
    host = FakeHost(fight_scene(80, 3))

    assert evaluate_number(host, arithmetic_module(), "plain") == -2.5 + 2 * 3 - 3
    assert evaluate_number(host, arithmetic_module(), "plain", {"weight": 1}) == 2


@pytest.mark.parametrize(("value", "expected"), [(1.7, 1), (-0.3, 0), (0.25, 0.25)])
def test_min_and_max_clamp_a_value(value: float, expected: float) -> None:
    host = FakeHost(fight_scene(80, 3))

    assert evaluate_number(host, arithmetic_module(), "clamped", {"value": value}) == expected


def test_dividing_by_zero_stops_with_its_own_code() -> None:
    host = FakeHost(fight_scene(80, 3))

    with pytest.raises(MacroFailure) as failed:
        evaluate_number(host, arithmetic_module(), "ratio", {"a": 1, "b": 0})

    assert failed.value.code == DIVIDED_BY_ZERO
    assert failed.value.payload["number_def"] == "ratio step 1"


@pytest.mark.parametrize(("hp", "passed"), [(20, True), (60, False)], ids=["lost 3/4", "lost 1/4"])
def test_arithmetic_over_a_reader_drives_a_require(hp: int, passed: bool) -> None:
    result = asyncio.run(run_macro(FakeHost(fight_scene(hp, 3)), arithmetic_module(), {}, 2))

    assert result.passed is passed
    if not passed:
        assert result.failure.code == SCENE_MISMATCH
        assert result.failure.payload["observed"]["lost()"] == "0.25"
