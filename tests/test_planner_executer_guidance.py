"""행동 지침 모드: planner 는 행동을 쓰지 않고, 등록 행동의 계약을 executer 가 집행한다.

`artel/docs/architecture/2026-11-01-pe-planner-guidance-only-design.md`. 불변식: planner 의 답에는 행동·준비·효과·대상이 없다,
대상은 등록 계약의 `holds` 멤버를 가진 지금 켜진 객체다, 준비 조건을 읽지 못한 후보는 선택지가 아니라 보류다.
"""

import asyncio

import pytest

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.contract import contract_json, registered_contract
from app.agents.planner_executer.developer_macros import DeveloperMacro, bind_guidance_calls, load_developer_macros
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.planner import split_guidance_block, split_guidance_summary_block
from app.agents.planner_executer.macros import RegisteredMacros, parse_guidance_step
from app.agents.qa.macro.errors import MacroRejection
from app.api.macros import MacroCheckRequest, check_macro
from app.qa.jev import JevClient
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import FakeJev

STRIKE = '''def strike(enemy: object) -> None:
    """Strike one living enemy so it loses health. Ends the player's move toward clearing the wave."""
    click(enemy)


def ready_strike(enemy: object) -> bool:
    return member(enemy, "Combat.Enemy::Hp") > 0


def effect_strike(enemy: object) -> bool:
    return member(enemy, "Enemy.hit") == True
'''
CONTRACT = {"params": {"enemy": {"holds": "Combat.Enemy::Hp"}}, "effect": {"mode": "transition", "within": 0.5}}
DONE = 'def done() -> bool:\n    return scene() == "Done"\n'


def _holder(index: int, hp: int, active: bool = True) -> dict:
    return {"path": "Enemy(Clone)", "id": 100 + index, "selector": f"Enemy(Clone)[{index}]", "active": active, "value": hp}


def _battle() -> FakeGame:
    """쓰러진 `Enemy(Clone)[0]` 은 꺼져 reading 에 없고, 살아 있는 둘이 path 하나를 나눠 가진다."""
    fake = FakeGame(
        "Battle",
        {
            f"Enemy(Clone)[{index}]": {"path": "Enemy(Clone)", "id": 100 + index, "members": {"Enemy.hit": False}}
            for index in (1, 2)
        },
    )
    holders = [_holder(0, 0, active=False), _holder(1, 10), _holder(2, 20)]
    fake.answers["read_member"] = lambda params: {"values": holders} if params == ["Combat.Enemy::Hp"] else {"values": []}

    def hit(game: FakeGame) -> None:
        game.objects["Enemy(Clone)[1]"]["members"]["Enemy.hit"] = True

    fake.reactions[("press", "Enemy(Clone)[1]")] = hit
    return fake


def _play(fake: FakeGame, jev: FakeJev, guidance: str | None = "Strike the weaker enemy first.") -> StepPlay:
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2, state_source="pulse_memory", max_turns_per_step=1)

    async def go() -> StepPlay:
        fake.push_reading()
        loaded = await load_developer_macros((DeveloperMacro("strike", STRIKE, "1.0", None, False, CONTRACT),))
        actions, contracts, binder = await bind_guidance_calls(["strike"], loaded)
        macros = await parse_guidance_step(DONE, RegisteredMacros(fake.channel), actions, contracts, binder, guidance)
        return await Executer(fake.channel, spec, JevClient(post=jev)).play(1, "Clear the wave.", macros)

    return asyncio.run(go())


def test_every_live_holder_is_an_option_and_only_the_picked_one_gets_the_input() -> None:
    fake = _battle()
    jev = FakeJev(picks=("developer:strike @ Enemy(Clone)[1]",))

    turn = _play(fake, jev).turns[0]

    assert {"developer:strike @ Enemy(Clone)[1]", "developer:strike @ Enemy(Clone)[2]"} <= set(turn["options"])
    assert not any("Enemy(Clone)[0]" in option for option in turn["options"]), "a switched-off holder is no option"
    assert turn["question"].endswith("How to play it: Strike the weaker enemy first.")
    pressed = [params[0] for method, params in fake.actions if method == "move_mouse"]
    assert pressed == ["Enemy(Clone)[1]"]
    assert turn["result"] == "effect_met" and turn["outcome"] == "passed"


def test_a_target_whose_readiness_cannot_be_read_is_held_not_offered() -> None:
    fake = _battle()
    calls = []

    def answer(params: list) -> dict:
        # 후보를 찾는 첫 물음에는 holder 가 오지만, 준비 조건이 다시 물으면 게임이 그 멤버를 읽지 못한다고 답한다.
        calls.append(params)
        if len(calls) == 1:
            return {"values": [_holder(1, 10), _holder(2, 20)]}
        return {"values": [], "unresolvable": [{"name": "Combat.Enemy::Hp", "reason": "the getter threw"}]}

    fake.answers["read_member"] = answer
    turn = _play(fake, FakeJev(picks=())).turns[0]

    assert not any(option.startswith("developer:strike") for option in turn.get("options") or [])
    assert turn["held_targets"] == [
        "developer:strike @ Enemy(Clone)[1] (unknown)", "developer:strike @ Enemy(Clone)[2] (unknown)"
    ]
    assert [params[0] for method, params in fake.actions if method == "move_mouse"] == []


def test_a_registered_contract_names_what_its_objects_hold_and_refuses_choosing_or_importing() -> None:
    contract = registered_contract("strike", CONTRACT, STRIKE)
    assert contract.holds == {"enemy": "Combat.Enemy::Hp"} and contract.has_ready and contract.effect.mode == "transition"
    assert contract_json(contract)["params"] == {"enemy": {"holds": "Combat.Enemy::Hp"}}

    loop = STRIKE.replace("    click(enemy)\n", '    for one in find_all(name="Enemy(Clone)"):\n        click(one)\n', 1)
    with pytest.raises(MacroRejection, match="loops over objects"):
        registered_contract("strike", CONTRACT, loop)
    with pytest.raises(MacroRejection, match="self-contained"):
        registered_contract("strike", CONTRACT, "from macros import other\n\n\n" + STRIKE)
    with pytest.raises(MacroRejection, match="holds"):
        registered_contract("strike", {"params": {"enemy": ["Enemy(Clone)"]}, "effect": CONTRACT["effect"]}, STRIKE)


def test_the_check_returns_the_contract_it_checked_and_a_plain_check_is_unchanged() -> None:
    plain = asyncio.run(check_macro(MacroCheckRequest(name="strike", source=STRIKE)))
    assert plain.ok and plain.contract is None

    checked = asyncio.run(check_macro(MacroCheckRequest(name="strike", source=STRIKE, contract=CONTRACT)))
    assert checked.ok and checked.contract["params"] == {"enemy": {"holds": "Combat.Enemy::Hp"}}
    assert checked.contract["revision"]

    refused = asyncio.run(check_macro(MacroCheckRequest(name="strike", source=STRIKE, contract={"params": {}})))
    assert not refused.ok and "params" in refused.rejection.reason


def test_a_guidance_plan_has_no_actions_no_conditions_and_no_objects() -> None:
    async def go():
        fake = _battle()
        loaded = await load_developer_macros((DeveloperMacro("strike", STRIKE, "1.0", None, False, CONTRACT),))
        with pytest.raises(MacroRejection, match="no arguments"):
            await bind_guidance_calls(['strike(find(name="Enemy(Clone)"))'], loaded)
        with pytest.raises(MacroRejection, match="not a registered action"):
            await bind_guidance_calls(["fly"], loaded)
        actions, contracts, binder = await bind_guidance_calls(["strike"], loaded)
        registered = RegisteredMacros(fake.channel)
        written = DONE + '\n\ndef press() -> None:\n    """Press it."""\n    click(selector("Enemy(Clone)[1]"))\n'
        with pytest.raises(MacroRejection, match="you do not write actions"):
            await parse_guidance_step(written, registered, actions, contracts, binder, None)
        conditioned = DONE + '\n\ndef ready_strike() -> bool:\n    return True\n'
        with pytest.raises(MacroRejection, match="registered action"):
            await parse_guidance_step(conditioned, registered, actions, contracts, binder, None)

        uncontracted = await load_developer_macros((DeveloperMacro("strike", STRIKE, "1.0", None, False),))
        with pytest.raises(MacroRejection, match="without a contract"):
            await bind_guidance_calls(["strike"], uncontracted)

    asyncio.run(go())


def test_the_guidance_for_people_is_its_own_block_and_leaves_the_guidance_for_the_decision_model() -> None:
    answer = "```guidance\nStrike living enemies first.\n```\n```guidance_summary\n1. 살아 있는 적부터 공격한다.\n```\n"

    rest, shown = split_guidance_summary_block(answer)
    rest, guidance = split_guidance_block(rest)

    assert shown == "1. 살아 있는 적부터 공격한다.\n" and guidance == "Strike living enemies first.\n"
    assert "```" not in rest.strip()
