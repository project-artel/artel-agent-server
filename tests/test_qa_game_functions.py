"""게임이 등록한 cheat 와 behavior 를 부르는 tool (ARTEL-1054).

계약은 workspace 의 `.plan/general/2026-10-14-game-functions-wire-contract.md` 다. 여기서 지키는
것은 wire 에 무엇이 나가는가, 무엇이 아무것도 안 보내고 거절되는가, 그리고 모델이 무엇을
읽는가다.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.agents.qa.arch import (
    QaArchSpec,
    VisionMode,
    default_resolved_arch,
    PhaseCycleMode,
    resolve_arch,
    structure_of,
    withheld_skills,
)
from app.agents.qa.runner import _game_functions
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.game_function_tools import focus
from app.agents.qa.tools.tool_context import ToolContext
from app.main import app
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType, StatusPayload, StepStatus
from app.qa.game_functions import (
    CaseFunctions,
    GameFunctionCatalog,
    GameFunctionEntry,
    allowed_names,
    resolve_target,
)
from app.qa.pulse import PulseMemory, PulseReading
from app.qa.schemas import QaRunScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore
from tests.test_qa_prompt_version import make_scenario, open_request

PLAYER = "Root[0]/Players[2]/Player(Clone)[1]"
OTHER_PLAYER = "Root[0]/Players[2]/Player(Clone)[2]"

GIVE_GOLD = {
    "name": "give_gold",
    "description": "Adds gold to the player's wallet.",
    "params": [{"name": "amount", "type": "int", "min": 1, "max": 1000000}],
    "target": None,
    "group": None,
}
HEAL = {"name": "heal", "params": [], "target": {"component": "PlayerController"}}
AUTO_ATTACK = {
    "name": "auto_attack",
    "params": [{"name": "range", "type": "float"}],
    "target": {"component": "PlayerController"},
    "group": "combat",
}
FLEE = {"name": "flee", "params": [], "target": None}


def _arch(cheats: str = "on", behaviors: str = "on"):
    return default_resolved_arch().model_copy(
        update={"phase_cycle": PhaseCycleMode.off, "cheats": cheats, "behaviors": behaviors}
    )


def make(
    cheats: str = "on",
    behaviors: str = "on",
    catalog: GameFunctionCatalog | None = None,
    step_functions: list[CaseFunctions | None] | None = None,
    step_meta: list[tuple[int | None, bool]] | None = None,
    total_steps: int = 2,
):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(
        qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05, reading_wait=0.01
    )
    channel.game_functions = catalog or GameFunctionCatalog(
        cheats=[GameFunctionEntry(**GIVE_GOLD), GameFunctionEntry(**HEAL)],
        behaviors=[GameFunctionEntry(**AUTO_ATTACK), GameFunctionEntry(**FLEE)],
    )
    state = QaRunState(
        total_steps=total_steps,
        step_meta=step_meta or [(1, False), (1, True)],
        step_functions=step_functions,
    )
    arch = _arch(cheats, behaviors)
    tools = {one.name: one for one in build_tools(channel, state, arch=arch)}
    return channel, state, tools, sent, arch


def actions(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.ACTION.value]


def answer(channel: QaRunChannel, sent: list[dict], result: dict):
    already = len(actions(sent))

    async def reply() -> None:
        for _ in range(100):
            if len(actions(sent)) > already:
                break
            await asyncio.sleep(0)
        channel.on_action_result(
            {"correlationId": actions(sent)[-1]["messageId"], "payload": {"results": [result]}}
        )

    return asyncio.create_task(reply())


def pulse(channel: QaRunChannel, **fields) -> None:
    payload = {"reading": channel.scene.pulse.readings + 1, "scene": "Field", **fields}
    channel.on_pulse({"payload": payload})


def players_listed(channel: QaRunChannel) -> None:
    pulse(
        channel,
        whole=True,
        functions={
            "whole": True,
            "entries": [
                {"kind": "cheat", "name": "give_gold", "target": None, "available": True},
                {"kind": "cheat", "name": "heal", "target": PLAYER, "available": True},
                {"kind": "cheat", "name": "heal", "target": OTHER_PLAYER, "available": True},
                {"kind": "behavior", "name": "auto_attack", "target": PLAYER, "available": True},
                {
                    "kind": "behavior",
                    "name": "flee",
                    "target": None,
                    "available": False,
                    "reason": "InBattle returned false",
                },
            ],
        },
    )


# --- the structure --------------------------------------------------------------


def _resolved(**overrides):
    return resolve_arch(QaArchSpec(vision=VisionMode.on, **overrides), "openai/gpt-6-luna")


def test_both_axes_default_to_off_and_add_no_tool() -> None:
    off_tools, _, off_print = structure_of(_resolved())
    assert QaArchSpec().cheats == "off" and QaArchSpec().behaviors == "off"
    for name in ("run_cheat", "start_behavior", "stop_behavior", "extend_behavior"):
        assert name not in off_tools
    assert off_print != structure_of(_resolved(cheats="on"))[2]


def test_the_tools_join_the_end_of_the_list_in_a_fixed_order() -> None:
    """목록의 순서가 계약이다. 축이 꺼진 run 의 목록은 앞부분 그대로 남는다."""
    off_tools = list(structure_of(_resolved())[0])
    both = list(structure_of(_resolved(cheats="on", behaviors="on"))[0])
    only_behaviors = list(structure_of(_resolved(behaviors="on"))[0])

    # `compact_context` 는 `structure_of` 가 `build_tools` 뒤에 붙인다. 새 tool 은 `build_tools` 의
    # 맨 뒤, 그 앞이다.
    assert off_tools[-1] == "compact_context"
    assert both == off_tools[:-1] + [
        "run_cheat",
        "start_behavior",
        "stop_behavior",
        "extend_behavior",
        "compact_context",
    ]
    assert only_behaviors == off_tools[:-1] + [
        "start_behavior",
        "stop_behavior",
        "extend_behavior",
        "compact_context",
    ]


def test_the_call_budget_grows_only_for_an_axis_that_is_on() -> None:
    off = _resolved()
    rationed_off = _resolved(max_cheats_per_run=3, max_behavior_calls_per_run=4)
    rationed_on = _resolved(
        cheats="on", behaviors="on", max_cheats_per_run=3, max_behavior_calls_per_run=4
    )

    assert rationed_off.tool_call_limit(2) == off.tool_call_limit(2)
    assert rationed_on.tool_call_limit(2) == off.tool_call_limit(2) + 7


def test_a_skill_is_withheld_while_its_axis_is_off() -> None:
    assert {"cheats", "behaviors"} <= withheld_skills(_resolved())
    assert "cheats" not in withheld_skills(_resolved(cheats="on"))
    assert "behaviors" not in withheld_skills(_resolved(behaviors="on"))


# --- permission and target ------------------------------------------------------


@pytest.mark.parametrize(
    ("permission", "expected"),
    [
        (None, ["a", "b"]),
        ("all", ["a", "b"]),
        ("none", []),
        ([], []),
        (["b", "not_in_run"], ["b"]),
    ],
)
def test_a_case_narrows_the_run_list_and_never_widens_it(permission, expected) -> None:
    assert allowed_names(["a", "b"], permission) == expected


def test_a_partial_target_resolves_only_to_one_match() -> None:
    assert resolve_target("Clone)[1]", [PLAYER, OTHER_PLAYER]) == (PLAYER, None)
    assert resolve_target(PLAYER, [PLAYER, OTHER_PLAYER]) == (PLAYER, None)

    none, why = resolve_target("Enemy", [PLAYER, OTHER_PLAYER])
    assert none is None and "No target matches" in why

    several, why = resolve_target("player(clone)", [PLAYER, OTHER_PLAYER])
    assert several is None and "2 targets match" in why and OTHER_PLAYER in why


# --- run_cheat ------------------------------------------------------------------


def test_a_cheat_goes_out_as_invoke_cheat_and_answers_with_the_games_message() -> None:
    async def run() -> None:
        channel, state, tools, sent, _ = make()
        reply = answer(
            channel, sent, {"id": 1, "success": True, "returnValue": {"message": "gold=1500"}}
        )
        result = await tools["run_cheat"].ainvoke(
            {"step": 1, "name": "give_gold", "arguments": {"amount": 500}, "thought": "precondition"}
        )
        await reply

        [frame] = actions(sent)
        [action] = frame["payload"]["actions"]
        assert action["method"] == "invoke_cheat"
        assert action["params"] == [{"name": "give_gold", "args": {"amount": 500}, "target": None}]
        assert "invoke_cheat: ok — gold=1500" in result
        assert [one.model_dump() for one in state.used_cheats] == [
            {"name": "give_gold", "args": {"amount": 500}, "target": None, "success": True}
        ]

    asyncio.run(run())


def test_a_partial_target_goes_out_as_the_full_selector() -> None:
    async def run() -> None:
        channel, state, tools, sent, _ = make()
        players_listed(channel)
        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"message": None}})
        await tools["run_cheat"].ainvoke(
            {"step": 1, "name": "heal", "target": "Clone)[2]", "thought": "회복"}
        )
        await reply

        assert actions(sent)[0]["payload"]["actions"][0]["params"][0]["target"] == OTHER_PLAYER
        assert state.used_cheats[0].target == OTHER_PLAYER

    asyncio.run(run())


def test_a_failed_cheat_is_recorded_as_failed() -> None:
    async def run() -> None:
        channel, state, tools, sent, _ = make()
        reply = answer(
            channel, sent, {"id": 1, "success": False, "error": "bad argument amount: below 1"}
        )
        result = await tools["run_cheat"].ainvoke(
            {"step": 1, "name": "give_gold", "arguments": {"amount": 0}, "thought": "x"}
        )
        await reply

        assert "FAILED — bad argument amount: below 1" in result
        assert state.used_cheats[0].success is False

    asyncio.run(run())


@pytest.mark.parametrize(
    ("call", "says"),
    [
        ({"name": "unknown_cheat"}, "is not a cheat this run was given"),
        ({"name": "give_gold", "target": "Player"}, "is static and takes no target"),
        ({"name": "heal", "target": "Player(Clone)"}, "2 targets match"),
        ({"name": "heal", "target": "Enemy"}, "No target matches"),
    ],
)
def test_a_refused_cheat_sends_nothing(call, says) -> None:
    async def run() -> None:
        channel, state, tools, sent, _ = make()
        players_listed(channel)
        result = await tools["run_cheat"].ainvoke({"step": 1, "thought": "x", **call})

        assert says in result
        assert actions(sent) == []
        assert state.used_cheats == []

    asyncio.run(run())


def test_a_case_that_does_not_allow_the_cheat_is_refused_naming_what_it_allows() -> None:
    async def run() -> None:
        _, _, tools, sent, _ = make(
            step_functions=[CaseFunctions(cheats=["heal", "not_in_run"]), None]
        )
        result = await tools["run_cheat"].ainvoke(
            {"step": 1, "name": "give_gold", "thought": "x"}
        )

        assert "Test case 1 does not allow the cheat `give_gold`" in result
        assert "It allows: heal." in result
        assert actions(sent) == []

    asyncio.run(run())


def test_a_case_that_allows_no_cheats_says_so() -> None:
    async def run() -> None:
        _, _, tools, _, _ = make(step_functions=[CaseFunctions(cheats="none"), None])
        result = await tools["run_cheat"].ainvoke({"step": 1, "name": "heal", "thought": "x"})
        assert "It allows no cheats." in result

    asyncio.run(run())


def test_a_run_with_the_axis_on_and_no_catalog_refuses_every_cheat() -> None:
    async def run() -> None:
        _, _, tools, sent, _ = make(catalog=GameFunctionCatalog())
        result = await tools["run_cheat"].ainvoke({"step": 1, "name": "heal", "thought": "x"})
        assert "No cheats were given to this run" in result
        assert actions(sent) == []

    asyncio.run(run())


def test_the_cheat_allowance_is_counted_in_attempts() -> None:
    async def run() -> None:
        channel, state, tools, sent, arch = make()
        state.cheats_attempted = arch.max_cheats_per_run
        result = await tools["run_cheat"].ainvoke({"step": 1, "name": "heal", "thought": "x"})
        assert "You have used all" in result
        assert actions(sent) == []

    asyncio.run(run())


# --- behaviors ------------------------------------------------------------------


def test_starting_a_behavior_sends_max_seconds_and_shows_it_running_at_once() -> None:
    async def run() -> None:
        channel, _, tools, sent, _ = make()
        players_listed(channel)
        reply = answer(
            channel, sent, {"id": 1, "success": True, "returnValue": {"behaviorId": 7, "report": None}}
        )
        result = await tools["start_behavior"].ainvoke(
            {
                "step": 1,
                "name": "auto_attack",
                "arguments": {"range": 8},
                "target": "Clone)[1]",
                "max_seconds": 60,
                "thought": "계속 공격",
            }
        )
        await reply

        assert actions(sent)[0]["payload"]["actions"][0]["params"] == [
            {"name": "auto_attack", "args": {"range": 8}, "target": PLAYER, "maxSeconds": 60}
        ]
        assert "started as #7, up to 60s" in result
        assert [one.behavior_id for one in channel.scene.pulse.behaviors] == [7]

    asyncio.run(run())


@pytest.mark.parametrize("seconds", [0, 301])
def test_max_seconds_out_of_range_is_refused_before_sending(seconds) -> None:
    async def run() -> None:
        _, _, tools, sent, _ = make()
        result = await tools["start_behavior"].ainvoke(
            {"step": 1, "name": "flee", "max_seconds": seconds, "thought": "x"}
        )
        assert "must be 1 to 300" in result
        assert actions(sent) == []

    asyncio.run(run())


def test_a_behavior_the_case_does_not_allow_is_refused() -> None:
    async def run() -> None:
        _, _, tools, sent, _ = make(step_functions=[CaseFunctions(behaviors=["flee"]), None])
        result = await tools["start_behavior"].ainvoke(
            {"step": 1, "name": "auto_attack", "thought": "x"}
        )
        assert "does not allow the behavior `auto_attack`" in result
        assert "It allows: flee." in result
        assert actions(sent) == []

    asyncio.run(run())


def test_stopping_takes_one_id_or_all_and_never_both() -> None:
    async def run() -> None:
        channel, _, tools, sent, _ = make()
        channel.scene.pulse.apply(
            PulseReading.model_validate(
                {"reading": 1, "behaviors": [{"behaviorId": 7, "name": "auto_attack", "seconds": 3}]}
            )
        )

        neither = await tools["stop_behavior"].ainvoke({"step": 1, "thought": "x"})
        both = await tools["stop_behavior"].ainvoke(
            {"step": 1, "behavior_id": 7, "all": True, "thought": "x"}
        )
        assert "Pass either" in neither and "Pass either" in both
        assert actions(sent) == []

        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"stopped": [7]}})
        result = await tools["stop_behavior"].ainvoke(
            {"step": 1, "behavior_id": 7, "thought": "멈춘다"}
        )
        await reply
        assert actions(sent)[0]["payload"]["actions"][0]["params"] == [
            {"behaviorId": 7, "reason": "stopped"}
        ]
        assert "stopped #7" in result
        assert channel.scene.pulse.behaviors == []

        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"stopped": []}})
        await tools["stop_behavior"].ainvoke({"step": 1, "all": True, "thought": "x"})
        await reply
        assert actions(sent)[1]["payload"]["actions"][0]["params"] == [
            {"all": True, "reason": "stopped"}
        ]

    asyncio.run(run())


def test_extending_sends_the_seconds_and_reports_the_new_limit() -> None:
    async def run() -> None:
        channel, _, tools, sent, _ = make()
        refused = await tools["extend_behavior"].ainvoke(
            {"step": 1, "behavior_id": 7, "seconds": 0, "thought": "x"}
        )
        assert "at least 1" in refused and actions(sent) == []

        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"maxSeconds": 90}})
        result = await tools["extend_behavior"].ainvoke(
            {"step": 1, "behavior_id": 7, "seconds": 60, "thought": "x"}
        )
        await reply
        assert actions(sent)[0]["payload"]["actions"][0]["params"] == [
            {"behaviorId": 7, "seconds": 60}
        ]
        assert "#7 now runs up to 90s" in result

    asyncio.run(run())


def test_a_behavior_ending_rides_on_the_next_tool_result_once() -> None:
    async def run() -> None:
        channel, _, tools, _, _ = make()
        channel.scene.pulse.apply(
            PulseReading.model_validate(
                {"reading": 1, "behaviors": [{"behaviorId": 7, "name": "auto_attack", "seconds": 3}]}
            )
        )
        channel.on_behavior_ended(
            {
                "payload": {
                    "behaviorId": 7,
                    "name": "auto_attack",
                    "target": PLAYER,
                    "reason": "completed",
                    "detail": "no enemy in range",
                    "seconds": 12.4,
                    "frame": 18234,
                }
            }
        )
        assert channel.scene.pulse.behaviors == []

        # 거절 결과에도 붙는다 — `answer` 를 안 지나는 결과다.
        first = await tools["extend_behavior"].ainvoke(
            {"step": 1, "behavior_id": 7, "seconds": 0, "thought": "x"}
        )
        second = await tools["extend_behavior"].ainvoke(
            {"step": 1, "behavior_id": 7, "seconds": 0, "thought": "x"}
        )
        line = (
            f"Behavior ended: #7 auto_attack on {PLAYER} — completed (no enemy in range) "
            "after 12.4s"
        )
        assert line in first
        assert "Behavior ended" not in second

    asyncio.run(run())


def test_the_service_accepts_behavior_ended() -> None:
    from tests.test_qa_service_deliver import _running_service

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            handled = service.deliver(
                session_id,
                {"type": "BEHAVIOR_ENDED", "payload": {"behaviorId": 7, "reason": "timeout"}},
            )
            assert handled is True
        finally:
            task.cancel()

    asyncio.run(run())


# --- report_step ----------------------------------------------------------------


def _status_frames(sent: list[dict]) -> list[dict]:
    return [frame["payload"] for frame in sent if frame["type"] == MessageType.STATUS.value]


def test_report_step_writes_used_cheats_and_active_behaviors_itself() -> None:
    async def run() -> None:
        channel, state, tools, sent, _ = make()
        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"message": "ok"}})
        await tools["run_cheat"].ainvoke(
            {"step": 1, "name": "give_gold", "arguments": {"amount": 5}, "thought": "x"}
        )
        await reply
        channel.scene.pulse.apply(
            PulseReading.model_validate(
                {
                    "reading": 1,
                    "behaviors": [
                        {
                            "behaviorId": 7,
                            "name": "auto_attack",
                            "target": PLAYER,
                            "seconds": 8.0,
                            "maxSeconds": 60,
                            "report": "hp=4",
                        }
                    ],
                }
            )
        )

        result = await tools["report_step"].ainvoke(
            {"step": 1, "performed": True, "passed": True, "message": "ok", "thought": "x"}
        )

        [status] = _status_frames(sent)
        assert status["used_cheats"] == [
            {"name": "give_gold", "args": {"amount": 5}, "target": None, "success": True}
        ]
        assert status["active_behaviors"] == [
            {"behaviorId": 7, "name": "auto_attack", "target": PLAYER, "seconds": 8.0}
        ]
        assert "Still running:" in result and "#7 auto_attack" in result
        # 다음 `report_step` 은 그 사이에 쓴 cheat 만 싣는다.
        assert state.used_cheats == []

    asyncio.run(run())


def test_a_run_with_both_axes_off_sends_status_without_the_two_keys() -> None:
    async def run() -> None:
        _, _, tools, sent, _ = make(cheats="off", behaviors="off")
        await tools["report_step"].ainvoke(
            {"step": 1, "performed": True, "passed": True, "message": "ok", "thought": "x"}
        )
        [status] = _status_frames(sent)
        assert "used_cheats" not in status
        assert "active_behaviors" not in status

    asyncio.run(run())


def test_status_payload_drops_only_the_unset_function_fields() -> None:
    plain = StatusPayload(status=StepStatus.COMPLETED, message="m").model_dump()
    assert "used_cheats" not in plain and "active_behaviors" not in plain
    assert plain["result"] is None
    filled = StatusPayload(status=StepStatus.COMPLETED, message="m", used_cheats=[]).model_dump()
    assert filled["used_cheats"] == []


def test_a_behavior_the_next_case_does_not_allow_is_stopped_before_its_first_step() -> None:
    async def run() -> None:
        channel, _, tools, sent, _ = make(
            step_meta=[(1, True), (2, True)],
            step_functions=[None, CaseFunctions(behaviors=["flee"])],
        )
        channel.scene.pulse.apply(
            PulseReading.model_validate(
                {
                    "reading": 1,
                    "behaviors": [
                        {"behaviorId": 7, "name": "auto_attack", "seconds": 3},
                        {"behaviorId": 8, "name": "flee", "seconds": 1},
                    ],
                }
            )
        )
        reply = answer(channel, sent, {"id": 1, "success": True, "returnValue": {"stopped": [7]}})
        result = await tools["report_step"].ainvoke(
            {"step": 1, "performed": True, "passed": True, "message": "ok", "thought": "x"}
        )
        await reply

        [frame] = actions(sent)
        assert frame["payload"]["step"] == 2
        assert frame["payload"]["actions"][0]["method"] == "stop_behavior"
        assert frame["payload"]["actions"][0]["params"] == [
            {"behaviorId": 7, "reason": "not_allowed"}
        ]
        assert "Stopped #7 auto_attack: test case 2 does not allow it." in result
        assert [one.behavior_id for one in channel.scene.pulse.behaviors] == [8]

    asyncio.run(run())


def test_no_stop_is_sent_inside_one_case() -> None:
    async def run() -> None:
        channel, _, tools, sent, _ = make(
            step_functions=[CaseFunctions(behaviors="none"), CaseFunctions(behaviors="none")]
        )
        channel.scene.pulse.apply(
            PulseReading.model_validate(
                {"reading": 1, "behaviors": [{"behaviorId": 7, "name": "auto_attack"}]}
            )
        )
        await tools["report_step"].ainvoke(
            {"step": 1, "performed": True, "passed": True, "message": "ok", "thought": "x"}
        )
        assert actions(sent) == []

    asyncio.run(run())


# --- what the model reads -------------------------------------------------------


def test_the_pulse_view_draws_now_and_not_now_for_what_the_case_allows() -> None:
    channel, state, _, _, arch = make(step_functions=[CaseFunctions(cheats=["heal"]), None])
    players_listed(channel)
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "reading": 2,
                "behaviors": [
                    {
                        "behaviorId": 7,
                        "name": "auto_attack",
                        "target": PLAYER,
                        "seconds": 12.4,
                        "maxSeconds": 60,
                        "report": "target=Slime(Clone)[3] hp=4",
                    }
                ],
            }
        )
    )
    focus(ToolContext(channel, state, arch), 1)
    view = channel.scene.pulse.render(since=0)

    assert f"Cheats now: heal on {PLAYER}, heal on {OTHER_PLAYER}" in view
    assert "give_gold" not in view
    assert f"Behaviors now: auto_attack on {PLAYER}" in view
    assert "Behaviors not now: flee (InBattle returned false)" in view
    assert (
        f"Behavior running: #7 auto_attack on {PLAYER}, 12.4s of 60s — "
        "target=Slime(Clone)[3] hp=4"
    ) in view


def test_the_pulse_view_draws_no_function_line_while_the_axes_are_off() -> None:
    memory = PulseMemory()
    plain = PulseMemory()
    reading = {
        "reading": 1,
        "whole": True,
        "functions": {
            "whole": True,
            "entries": [{"kind": "cheat", "name": "give_gold", "available": True}],
        },
        "behaviors": [{"behaviorId": 7, "name": "auto_attack"}],
    }
    memory.apply(PulseReading.model_validate(reading))
    plain.apply(PulseReading.model_validate({"reading": 1, "whole": True}))

    assert memory.render(since=0) == plain.render(since=0)


def test_pulse_functions_merge_remove_and_replace() -> None:
    memory = PulseMemory()
    memory.apply(
        PulseReading.model_validate(
            {
                "reading": 1,
                "functions": {
                    "whole": True,
                    "entries": [
                        {"kind": "behavior", "name": "flee", "target": PLAYER},
                        {"kind": "behavior", "name": "flee", "target": OTHER_PLAYER},
                    ],
                },
            }
        )
    )
    memory.apply(
        PulseReading.model_validate(
            {
                "reading": 2,
                "functions": {
                    "whole": False,
                    "entries": [
                        {"kind": "behavior", "name": "flee", "target": OTHER_PLAYER, "removed": True},
                        {"kind": "behavior", "name": "flee", "target": PLAYER, "available": False},
                    ],
                },
            }
        )
    )
    assert list(memory.functions) == [f"behavior|flee|{PLAYER}"]
    assert memory.functions[f"behavior|flee|{PLAYER}"].available is False

    memory.apply(
        PulseReading.model_validate(
            {"reading": 3, "functions": {"whole": True, "entries": [{"kind": "cheat", "name": "x"}]}}
        )
    )
    assert list(memory.functions) == ["cheat|x|"]
    # `functions` 가 안 실린 `PULSE` 는 쥔 것을 그대로 둔다.
    memory.apply(PulseReading.model_validate({"reading": 4}))
    assert list(memory.functions) == ["cheat|x|"]


def test_the_first_message_lists_the_catalog_of_the_axes_that_are_on() -> None:
    channel, _, _, _, _ = make()
    both = _game_functions(_arch("on", "on"), channel)
    assert "Cheats this run may call with `run_cheat`" in both
    assert "- give_gold(amount: int 1..1000000)  [static] — Adds gold to the player's wallet." in both
    assert "- auto_attack(range: float)  [on PlayerController, group combat]" in both

    only_cheats = _game_functions(_arch("on", "off"), channel)
    assert "Behaviors" not in only_cheats
    assert _game_functions(_arch("off", "off"), channel) == ""


# --- session open ---------------------------------------------------------------


def test_session_open_takes_the_catalog_and_the_two_arch_keys() -> None:
    service = QaExecutionService(store=InMemoryQaSessionStore())
    app.state.qa_session_service = service
    try:
        body = open_request(arch={"cheats": "on", "behaviors": "on"})
        body["context"]["cheats"] = [GIVE_GOLD]
        body["context"]["behaviors"] = [AUTO_ATTACK]
        response = TestClient(app).post("/internal/qa-sessions", json=body)
        assert response.status_code == 200, response.text
        config = response.json()["run_config"]
        assert config["arch"]["cheats"] == "on"
        tools = config["tools"]
        assert tools.index("run_cheat") < tools.index("start_behavior")
        assert {"stop_behavior", "extend_behavior"} <= set(tools)

        record = asyncio.run(service._load(response.json()["session_id"]))
        assert [one.name for one in record.cheats] == ["give_gold"]
        assert record.behaviors[0].target.component == "PlayerController"
    finally:
        del app.state.qa_session_service


def test_a_test_case_carries_functions() -> None:
    scenario = make_scenario().model_dump()
    scenario["steps"][0]["case"]["functions"] = {"cheats": ["give_gold"], "behaviors": "all"}
    item = QaRunScenario.model_validate(
        {"qa_try_id": 1, "test_scenario_id": 1, "scenario": scenario}
    )
    functions = item.scenario.steps[0].case.functions
    assert functions.cheats == ["give_gold"] and functions.behaviors == "all"
    # 키가 없으면 "all" 이다.
    assert CaseFunctions.model_validate({}).permission("behavior") == "all"


def test_active_behaviors_keeps_a_behavior_that_ended_before_the_verdict() -> None:
    # 2026-10-15 로컬 run: `auto_cast_spells` 가 적을 모두 처치하고 `report_step` 전에 끝나서,
    # 판정할 때 도는 것만 적던 `active_behaviors` 가 비었다.
    async def run() -> None:
        channel, _, tools, sent, _ = make()
        running = {
            "behaviorId": 1,
            "name": "auto_attack",
            "target": None,
            "seconds": 12.0,
            "maxSeconds": 30,
        }
        channel.scene.pulse.apply(PulseReading.model_validate({"reading": 1, "behaviors": [running]}))
        channel.on_behavior_ended(
            {"payload": {"behaviorId": 1, "name": "auto_attack", "reason": "completed", "seconds": 26.3}}
        )
        channel.scene.pulse.apply(PulseReading.model_validate({"reading": 2, "behaviors": []}))

        await tools["report_step"].ainvoke({"step": 1, "performed": True, "passed": True, "message": "ok", "thought": "x"})

        [status] = _status_frames(sent)
        assert status["active_behaviors"] == [
            {"behaviorId": 1, "name": "auto_attack", "target": None, "seconds": 26.3}
        ]

    asyncio.run(run())


def test_active_behaviors_counts_only_this_steps_share_of_a_carried_behavior() -> None:
    async def run() -> None:
        channel, _, tools, sent, _ = make()

        def pulse(reading: int, seconds: float) -> None:
            channel.scene.pulse.apply(
                PulseReading.model_validate(
                    {
                        "reading": reading,
                        "behaviors": [
                            {"behaviorId": 3, "name": "auto_attack", "seconds": seconds, "maxSeconds": 60}
                        ],
                    }
                )
            )

        pulse(1, 5.0)
        await tools["report_step"].ainvoke({"step": 1, "performed": True, "passed": True, "message": "ok", "thought": "x"})
        pulse(2, 9.0)
        await tools["report_step"].ainvoke({"step": 2, "performed": True, "passed": True, "message": "ok", "thought": "x"})

        first, second = _status_frames(sent)
        assert first["active_behaviors"] == [
            {"behaviorId": 3, "name": "auto_attack", "target": None, "seconds": 5.0}
        ]
        assert second["active_behaviors"] == [
            {"behaviorId": 3, "name": "auto_attack", "target": None, "seconds": 4.0}
        ]

    asyncio.run(run())
