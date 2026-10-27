"""executer 가 action 을 고를 때 보내는 거른 state(`jev_state="members"`)."""

import asyncio

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import Executer
from app.agents.planner_executer.jev_state import (
    MEMBER_HEAVY,
    JevStateFilter,
    assemble,
    changed_values,
    reads_of,
    referenced_names,
    unfiltered,
    watched_values,
)
from app.agents.planner_executer.macros import RegisteredMacros, first_sentence, parse_step
from app.agents.qa.macro.binding import MacroMemories
from app.qa.jev import JevClient
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, FakeJev

# member 가 많은 object 하나. `Panel.blocker` 는 다른 object 를 가리키는 참조라 80자를 넘는다.
HEAVY = {f"Panel.field{index}": index for index in range(MEMBER_HEAVY)} | {
    "Panel.blocker": {"path": "Root/Blocker", "active": True, "position": [1.0, 2.0, 3.0], "rect": [0, 0, 1920, 1080]},
}
OPTIONS = {"press_start": "Press Start.", "look_around": "Wait."}


class RelevanceJev(FakeJev):
    """relevance 질문은 `irrelevant` 에 든 글자가 질문에 있으면 0.0, 아니면 0.9 로 답한다."""

    def __init__(self, irrelevant: tuple[str, ...] = (), down: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.irrelevant = irrelevant
        self.down = down
        self.relevance: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        if "answer" in body["questions"]:
            return await super().__call__(body)
        self.relevance.append(body)
        if self.down:
            raise RuntimeError("Jev is down")
        return {"answers": {
            key: {"noul": 0.0 if any(word in question["instructions"] for word in self.irrelevant) else 0.9}
            for key, question in body["questions"].items()
        }}


def _game() -> FakeGame:
    return FakeGame("Menu", {
        "Root/Start": {"text": "Start"},
        "Root/Credits": {"text": "Credits"},
        "Root/Panel": {"members": HEAVY},
    })


def _filtered(jev: RelevanceJev, calls: int = 1):
    fake = _game()

    async def go():
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        state_filter = JevStateFilter(JevClient(post=jev))
        results = []
        reads = reads_of([MACROS])
        for _ in range(calls):
            memories = MacroMemories(scene=fake.channel.scene)
            objects, note = await state_filter.objects(memories, "Press Start.", macros, OPTIONS, reads)
            state, _assembled = assemble(memories, objects, reads)
            results.append((state, note))
        return results

    return asyncio.run(go())


def _paths(state: dict) -> list[str]:
    return [obj["path"] for obj in state["objects"]]


def test_the_plan_names_what_it_reads():
    assert referenced_names(MACROS) == {"Start"}


def test_an_object_jev_calls_irrelevant_is_dropped_and_a_pinned_one_is_never_asked():
    jev = RelevanceJev(irrelevant=("Credits", "Start"))

    [(state, note)] = _filtered(jev)

    # `find(name="Start")` 가 부르는 object 는 묻지 않고 남긴다. 그래서 "Start" 에 0.0 을 주게 해 두어도 남는다.
    assert "Root/Start" in _paths(state)
    assert "Root/Credits" not in _paths(state)
    asked = " ".join(q["instructions"] for body in jev.relevance for q in body["questions"].values())
    assert "`Root/Start`" not in asked
    assert note == {"live": 3, "pinned": 1, "dropped": 1}


def test_a_heavy_object_keeps_only_the_members_jev_needs_and_references_keep_active():
    jev = RelevanceJev(irrelevant=("field `Panel.field0`", "field `Panel.field1`"))

    [(state, _note)] = _filtered(jev)

    [panel] = [obj for obj in state["objects"] if obj["path"] == "Root/Panel"]
    assert "Panel.field0" not in panel["members"]
    assert panel["members"]["Panel.field2"] == 2
    # 원본은 80자에서 잘린 문자열이 되어 `active` 를 잃는다.
    assert panel["members"]["Panel.blocker"] == {"path": "Root/Blocker", "active": True}


def test_each_object_is_asked_once_per_plan():
    jev = RelevanceJev()

    _filtered(jev, calls=2)

    assert len(jev.relevance) == 2  # object 한 번, member 한 번. 두 번째 state 는 저장한 답을 쓴다.


def test_when_jev_cannot_answer_nothing_is_dropped():
    jev = RelevanceJev(down=True)

    [(state, _note)] = _filtered(jev)

    assert _paths(state) == ["Root/Start", "Root/Credits", "Root/Panel"]


def test_the_executer_picks_on_the_filtered_state_and_logs_what_it_sent(monkeypatch):
    monkeypatch.setattr(executer_module, "SETTLE_QUIET_SECONDS", 0.001)
    fake = _game()

    def to_map(game: FakeGame) -> None:
        game.scene = "Map"
        game.objects = {"Root/Stage": {"text": "Stage 0"}}

    fake.reactions[("press", "Root/Start")] = to_map
    jev = RelevanceJev(irrelevant=("Credits",))

    async def go():
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        spec = PlannerExecuterArchSpec(jev_state="members", no_action_grace_seconds=0.2)
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Press Start.", macros)

    play = asyncio.run(go())

    assert play.stopped == "done"
    [pick] = jev.asked("choice")
    assert "Root/Credits" not in _paths(pick["state"])
    assert play.turns[0]["jev_state"]["sent"] == 2
    assert play.turns[0]["state"] == {k: v for k, v in pick["state"].items() if k != "recent_picks"}
    # 목표 확인은 원본 state 로 묻는다.
    [goal] = jev.asked("noul")
    assert _paths(goal["state"]) == ["Root/Stage"]


def test_the_unfiltered_state_puts_what_the_actions_read_first_and_says_how_many_it_cut():
    """경로 알파벳순으로 뒤를 자르면 전투 화면의 `Card(Clone)` 이후가 통째로 빠졌다(try 192 감사에서 live object 의 72%)."""
    filler = {f"Root/A{index:02d}": {"members": {"Panel.note": "x" * 70}} for index in range(120)}
    fake = FakeGame("Menu", {**filler, "Root/Start": {"text": "Start"}})
    fake.push_reading()
    memories = MacroMemories(scene=fake.channel.scene)
    reads = reads_of([MACROS])

    state, note = assemble(memories, unfiltered(memories, reads), reads)

    assert _paths(state)[0] == "Root/Start"
    assert note["omitted"] > 0
    assert state["omitted_objects"] == note["omitted"]
    assert note["missing"] == []


DEVELOPER_BODY = """
def end_turn() -> None:
    if static("CombineZone.Instance.IsAwaitingTarget") == False:
        hand: object = selector("CardSystem/CardManager")
        before: int = member(hand, "CardManager.myCards.Count")
        click(selector("DebugCanvas/TurnEndButton"))
"""


def test_what_a_developer_macro_body_reads_is_pinned_and_its_static_is_sent():
    """try 193: 계획에는 `developer:end_turn` 이름만 있어 고정 0 이었고, static 은 어느 경로로도 가지 않았다."""
    filler = {f"Root/A{index:02d}": {"members": {"Panel.note": "x" * 70}} for index in range(120)}
    fake = FakeGame("TurnBattleScene", {**filler, "CardSystem/CardManager": {"members": {}}})
    fake.push_reading()
    fake.channel.on_pulse({"type": "PULSE", "payload": {
        "schema": 2, "reading": 99, "frame": 999, "scene": "TurnBattleScene", "whole": False,
        "active": [{"selector": "CardSystem/CardManager", "path": "CardSystem/CardManager",
                    "members": [{"on": "Cards.CardManager", "member": "myCards", "count": 5}]}],
        "statics": [
            {"declaring": "Combat.UI.CombineZone", "member": "Instance.IsAwaitingTarget", "value": False},
            {"declaring": "Core.InteractionLock", "member": "locked", "value": True},
        ],
        "changed": [],
    }})
    memories = MacroMemories(scene=fake.channel.scene)
    reads = reads_of(["def look() -> None:\n    pass\n", DEVELOPER_BODY])

    state, note = assemble(memories, unfiltered(memories, reads), reads)

    assert _paths(state)[0] == "CardSystem/CardManager"
    assert state["objects"][0]["members"] == {"CardManager.myCards.Count": 5}
    assert state["statics"] == {"CombineZone.Instance.IsAwaitingTarget": False, "InteractionLock.locked": True}
    assert note["required"] == 1 and note["missing"] == []


def test_features_are_added_to_the_objects_not_put_in_their_place():
    fake = _game()
    fake.push_reading()
    memories = MacroMemories(scene=fake.channel.scene)
    reads = reads_of([MACROS])

    state, _note = assemble(memories, unfiltered(memories, reads), reads, {"cards_in_hand": 5})

    assert state["features"] == {"cards_in_hand": 5}
    assert "Root/Start" in _paths(state)


def test_what_an_action_changed_is_read_off_the_values_the_actions_read():
    """try 193 의 턴 종료는 매번 성공했다. `changed` 가 없으면 Jev 는 성공과 진전을 가릴 수 없다."""
    fake = FakeGame("TurnBattleScene", {"Enemy": {"members": {"Enemy.hp": 10}}})
    fake.push_reading()
    reads = reads_of(['def hit() -> None:\n    if member(selector("Enemy"), "Enemy.hp") > 0:\n        click(selector("Enemy"))\n'])
    before = watched_values(MacroMemories(scene=fake.channel.scene), reads)
    fake.objects["Enemy"]["members"]["Enemy.hp"] = 7
    fake.push_reading()
    after = watched_values(MacroMemories(scene=fake.channel.scene), reads)

    assert changed_values(before, after, 8) == {"Enemy.Enemy.hp": [10, 7]}
    assert changed_values(after, after, 8) == {}


def test_a_label_is_the_first_paragraph_or_its_first_sentence_when_long():
    planner = "Press Start on the title screen. The right move while the menu shows Start.\n\nMore."
    developer = (
        "In TurnBattleScene on the player's turn, fills the combination window from the hand without choosing:\n"
        "an attack spell card (Shoot, else Drop, else Explode) and any element card. Then call cast_at_first_enemy\n"
        "(or cast_at). To play through a battle, repeat dismiss_dialogue, prepare_any_spell, cast_at_first_enemy."
    )

    assert first_sentence(planner) == "Press Start on the title screen. The right move while the menu shows Start."
    assert first_sentence(developer) == (
        "In TurnBattleScene on the player's turn, fills the combination window from the hand without choosing: "
        "an attack spell card (Shoot, else Drop, else Explode) and any element card."
    )


def test_what_an_action_looks_for_but_is_not_on_screen_is_said_and_heavy_required_objects_keep_read_members():
    """조합 버튼이 없다는 것은 준비된 주문이 없다는 뜻이다. 그리고 member 15개짜리 카드 8장이 상한을 다 쓰면 안 된다."""
    cards = {
        f"Card(Clone)[{index}]": {"members": {f"Card.field{n}": "x" * 30 for n in range(14)} | {"Card.cardType": "Shoot"}}
        for index in range(12)
    }
    fake = FakeGame("TurnBattleScene", {**cards, "DebugCanvas/TurnEndButton": {"text": "턴 종료"}})
    fake.push_reading()
    memories = MacroMemories(scene=fake.channel.scene)
    reads = reads_of([
        'def go() -> None:\n'
        '    for card in find_all(name="Card(Clone)"):\n'
        '        if member(card, "Card.cardType") == "Shoot":\n'
        '            click(card)\n'
        '    if exists(selector("CombineSystem/CombineZone/Button")):\n'
        '        click(selector("DebugCanvas/TurnEndButton"))\n'
    ])

    state, note = assemble(memories, unfiltered(memories, reads), reads)

    assert state["not_on_screen"] == ["CombineSystem/CombineZone/Button"]
    assert all(obj["members"] == {"Card.cardType": "Shoot"} for obj in state["objects"] if "Card" in obj["path"])
    assert note["missing"] == [] and note["omitted"] == 0
