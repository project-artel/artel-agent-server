"""행동 계약: 컴파일 검사와, 가짜 SDK 위에서 후보 → 준비 → 바인딩 → 실행 → 효과 판정의 불변식.

불변식(설계 문서): 고르지 않은 대상에 입력하지 않는다, 준비 조건이 거짓이거나 확인 불가면 실행하지 않는다, 입력이 갔다는
것만으로 성공하지 않는다, 이미 참이던 조건을 효과로 세지 않는다.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.contract import read_contracts
from app.agents.planner_executer.executer import ContractInstance, Executer, StepPlay
from app.agents.planner_executer.macro_host import ChannelMacroHost
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.qa.macro.errors import MacroRejection
from app.qa.envelope import JsonRpcAction
from app.qa.jev import JevClient
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import FakeJev

SOURCE = '''def take(card: object) -> None:
    """Take a free card from the table."""
    click(card)


def ready_take(card: object) -> bool:
    return member(card, "Card.free") == True


def effect_take(card: object) -> bool:
    return member(card, "Card.taken") == True


def done() -> bool:
    return scene() == "Done"
'''

ACTIONS = '{"take": {"params": {"card": ["Card"]}, "effect": {"mode": "transition", "within": 0.5}}}'


def _table(free=(True, True, False), taken=(False, False, False)) -> FakeGame:
    objects = {
        f"Card[{index}]": {"members": {"Card.free": free[index], "Card.taken": taken[index]}}
        for index in range(len(free))
    }
    return FakeGame("Table", objects)


def _take_reaction(fake: FakeGame, selector: str) -> None:
    def take(game: FakeGame) -> None:
        game.objects[selector]["members"]["Card.taken"] = True

    fake.reactions[("press", selector)] = take


def _play(fake: FakeGame, jev: FakeJev, source: str = SOURCE, actions: str = ACTIONS, **knobs) -> StepPlay:
    spec = PlannerExecuterArchSpec(
        **{"no_action_grace_seconds": 0.2, "state_source": "pulse_memory", "max_turns_per_step": 1, **knobs}
    )

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(source, RegisteredMacros(fake.channel), None, actions)
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Take a card.", macros)

    return asyncio.run(go())


def test_each_ready_object_is_its_own_option_and_only_the_picked_one_gets_the_input() -> None:
    fake = _table()
    _take_reaction(fake, "Card[1]")
    jev = FakeJev(picks=("take @ Card[1]",))

    play = _play(fake, jev)

    turn = play.turns[0]
    assert {"take @ Card[0]", "take @ Card[1]"} <= set(turn["options"]) and "take @ Card[2]" not in turn["options"]
    assert turn["not_ready_targets"] == ["take @ Card[2] (unmet)"]
    assert turn["picked"] == "take @ Card[1]" and turn["target"] == "Card[1]"
    pressed = [params[0] for method, params in fake.actions if method == "move_mouse"]
    assert pressed == ["Card[1]"]
    assert turn["result"] == "effect_met" and turn["outcome"] == "passed"


def test_an_input_that_lands_without_its_effect_is_not_a_success() -> None:
    fake = _table()  # no reaction: the click lands but nothing is taken
    play = _play(fake, FakeJev(picks=("take @ Card[0]",)))

    turn = play.turns[0]
    assert turn["outcome"] == "passed" and turn["result"] == "effect_unmet" and turn.get("misses") == 1


def test_a_condition_that_was_already_true_is_not_counted_as_the_effect() -> None:
    fake = _table(taken=(True, False, False))
    play = _play(fake, FakeJev(picks=("take @ Card[0]",)))

    assert play.turns[0]["result"] == "already_true" and not play.turns[0].get("misses")


def test_the_host_sends_nothing_to_an_object_outside_the_binding() -> None:
    fake = _table()

    async def go():
        fake.push_reading()
        host = ChannelMacroHost(fake.channel, JevClient(post=FakeJev()), 1, allowed_targets=frozenset({"Card[0]"}))
        outcome = await host.run(
            [JsonRpcAction(id=1, method="move_mouse", params=["Card[1]"]),
             JsonRpcAction(id=2, method="mouse_down", params=[0])],
            "click(card)", 1,
        )
        return host, outcome

    host, outcome = asyncio.run(go())

    assert host.contract_violation is not None and "Card[1]" in host.contract_violation
    assert "FAILED" in outcome.text and fake.actions == []


def test_a_bound_object_replaced_by_another_is_stale() -> None:
    held = {"a": SimpleNamespace(selector="Card[0]", path="Card", live=True, id=7)}
    stub = SimpleNamespace(_channel=SimpleNamespace(scene=SimpleNamespace(pulse=SimpleNamespace(held=held))))
    instance = ContractInstance("take", (("card", "Card[0]"),), {"Card[0]": 7}, "")

    assert Executer._alive(stub, instance)
    held["a"].id = 8  # same address, a new object
    assert not Executer._alive(stub, instance)
    held.clear()
    assert not Executer._alive(stub, instance)


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ('    for card in find_all(name="Card"):\n        click(card)\n', "loops over objects"),
        ('    while member(card, "Card.free") == True:\n        click(card)\n', "has a `while`"),
        ('    click(card)\n    wait_until(member(card, "Card.taken") == True, 3, "x")\n', "calls `wait_until`"),
        ('    other: object = find(name="Card")\n    click(other)\n', "calls `find`"),
    ],
)
def test_a_body_that_chooses_or_waits_is_refused(body: str, said: str) -> None:
    source = SOURCE.replace('    click(card)\n', body, 1)

    with pytest.raises(MacroRejection) as refused:
        read_contracts(ACTIONS, source)
    assert said in str(refused.value)


def test_a_contract_needs_its_effect_predicate_and_every_action_needs_a_contract() -> None:
    with pytest.raises(MacroRejection, match="needs `def effect_take"):
        read_contracts(ACTIONS, SOURCE.replace("def effect_take", "def effect_other"))
    extra = SOURCE + '\n\ndef wave() -> None:\n    """Wave."""\n    click(selector("Hand"))\n'
    with pytest.raises(MacroRejection, match="`wave` has no entry"):
        read_contracts(ACTIONS, extra)
    contracts = read_contracts(
        '{"take": {"params": {"card": ["Card"]}, "effect": {"mode": "transition"}}, '
        '"wave": {"params": {}, "effect": {"mode": "unobservable", "reason": "a wave changes nothing"}}}',
        extra,
    )
    assert contracts["wave"].fixed_targets == ("Hand",) and contracts["take"].domains == {"card": ("Card",)}


class Verbatim:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answers.pop(0)


def test_a_planner_in_contract_mode_answers_with_actions_and_the_step_passes_on_the_engines_judgement() -> None:
    from tests.test_planner_executer_steps import _judge, _menu, _run, _verdicts

    answer = (
        "```python\n"
        "def press(button: object) -> None:\n"
        '    """Press the menu button that leads on."""\n'
        "    click(button)\n\n\n"
        "def effect_press(button: object) -> bool:\n"
        '    return scene() == "Map"\n\n\n'
        "def done() -> bool:\n"
        '    return scene() == "Map"\n'
        "```\n"
        '```actions\n{"press": {"params": {"button": ["Start"]}, "effect": {"mode": "transition", "within": 1}}}\n```\n'
    )
    asked = Verbatim(answer)
    fake = _menu()
    judge, seen = _judge({"passed": True, "reason": "The map opened.", "state_detail": "", "insight": ""})

    state, failure = _run(fake, asked, judge, FakeJev(picks=("press @ Root/Start",)), actions="contract", analysis="off")

    assert failure is None and state.step_results[0].passed
    assert asked.calls[0][0].startswith("You write ONE test step") and "Actions are contracts" in asked.calls[0][0]
    turn = next(f["payload"]["turn"] for f in fake.frames("LOG") if f["payload"].get("kind") == "turn")
    assert turn["picked"] == "press @ Root/Start" and turn["result"] == "effect_met"
    decision = next(f["payload"]["decision"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_macros")
    assert decision["contracts"]["press"]["params"] == {"button": ["Start"]}
    assert _verdicts(fake)[0]["status"] == "COMPLETED"


def test_a_step_that_selects_only_developer_macros_needs_no_actions_block() -> None:
    only_done = 'def done() -> bool:\n    return scene() == "Map"\n'
    assert read_contracts(None, only_done) == {} and read_contracts("{}", only_done) == {}
