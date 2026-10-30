"""Executer 가 Jev 에게 보내는 state 를 Orchestration 의 `STATE_SET` 에서 읽는다 (ARTEL-1297).

대조 테스트의 fixture 둘은 같은 `pulse` 열에서 왔다. `wordventure-try205-pulses.jsonl.gz` 는 로컬 런 155(try 205)의
`SDK_TO_ORCHE` `pulse` 853개 원문이고, `wordventure-try205-state-set-checkpoints.jsonl.gz` 는 orchestration-server
의 reducer(ARTEL-1296, `StateSet`)가 그 열을 적용하다가 다섯 지점에서 `EXECUTER_SPEC` 으로 낸 snapshot 이다. 같은
지점까지 `PulseMemory` 에 적용해 그린 state 와 같아야 한다 — 출처만 바뀌고 Jev 가 읽는 글은 같다는 것이 이 변경의
전제다.
"""

import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.macros import RegisteredMacros, parse_step
from app.agents.qa.macro.binding import MacroMemories, MacroScope
from app.agents.qa.macro.context import build_context_state
from app.qa.jev import JevClient
from app.qa.pulse import PulseReading
from app.qa.scene import SceneMemory
from app.qa.state_set import (
    EXECUTER_SPEC,
    StateSetUnavailable,
    StateSnapshot,
    read_state,
    render_jev_state,
)
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, FakeJev, _menu

DATA = Path(__file__).parent / "data" / "state_set"


def _rows(path: str) -> list[dict]:
    with gzip.open(DATA / path, "rt") as stream:
        return [json.loads(line) for line in stream]


PULSES = _rows("wordventure-try205-pulses.jsonl.gz")
CHECKPOINTS = _rows("wordventure-try205-state-set-checkpoints.jsonl.gz")


@pytest.mark.parametrize("checkpoint", CHECKPOINTS, ids=lambda c: f"after-{c['after']}")
def test_state_set_and_pulse_memory_give_jev_the_same_state(checkpoint: dict) -> None:
    scene = SceneMemory()
    for row in PULSES[: checkpoint["after"] + 1]:
        scene.pulse.apply(PulseReading.model_validate(row))
    memories = MacroMemories(scene=scene)

    from_pulse_memory, _ = build_context_state(memories, MacroScope(), ())
    snapshot = StateSnapshot.model_validate(checkpoint["snapshot"])
    from_state_set, _ = render_jev_state(snapshot, scene.pulse.is_cosmetic, scene=memories.scene_name)

    assert snapshot.synced
    assert from_state_set == from_pulse_memory


def test_stale_and_inactive_objects_and_stale_members_are_left_out() -> None:
    snapshot = StateSnapshot.model_validate({
        "stateVersion": 3, "sync": "SYNCED", "scene": "Battle",
        "entities": [
            {"entityId": "s1:1", "scope": "scene:Battle#1", "path": "Player", "selector": "Player[0]",
             "active": True, "status": "KNOWN",
             "members": {
                 "Battle.Player::hp#0": {"status": "KNOWN", "value": 0},
                 "Battle.Player::mp#0": {"status": "STALE", "value": 9},
                 "Core.InteractionLock::IsLocked#0": {"status": "UNREAD", "reason": "property runs code"},
             },
             "fragments": {"text": {"status": "KNOWN", "value": "Me"}, "ui": {"status": "STALE", "value": {}}}},
            {"entityId": "s1:2", "scope": "scene:Battle#1", "path": "Old", "active": True, "status": "STALE"},
            {"entityId": "s1:3", "scope": "scene:Battle#1", "path": "Off", "active": False, "status": "KNOWN"},
        ],
    })

    state, clipped = render_jev_state(snapshot)

    assert clipped == 0
    assert state == {
        "scene": "Battle",
        "objects": [{
            "path": "Player[0]",
            "text": "Me",
            "members": {"InteractionLock.IsLocked": "(unread: property runs code)", "Player.hp": 0},
        }],
    }


def test_read_state_posts_the_spec_and_fails_as_one_type() -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/404/state"):
            return httpx.Response(404, json={"code": "not_found"})
        return httpx.Response(200, json={"stateVersion": 1, "sync": "AWAITING_WHOLE", "entities": []})

    async def go() -> tuple[StateSnapshot, Exception]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            snapshot = await read_state(7, EXECUTER_SPEC, base_url="http://orch/", client=client)
            try:
                await read_state(404, EXECUTER_SPEC, base_url="http://orch", client=client)
            except StateSetUnavailable as error:
                return snapshot, error
        raise AssertionError("a 404 must fail")

    snapshot, error = asyncio.run(go())

    assert seen[0].url == "http://orch/internal/qa-tries/7/state"
    assert json.loads(seen[0].content) == EXECUTER_SPEC
    assert snapshot.state_version == 1 and not snapshot.synced
    assert "404" in str(error)
    with pytest.raises(StateSetUnavailable, match="orchestration_base_url"):
        asyncio.run(read_state(7, EXECUTER_SPEC, base_url=""))


# ── Executer ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _quick(monkeypatch) -> None:
    monkeypatch.setattr(executer_module, "SETTLE_QUIET_SECONDS", 0.001)
    monkeypatch.setattr(executer_module, "IDLE_LOOK_SECONDS", 0.01)
    monkeypatch.setattr(executer_module, "STATE_SYNC_WAIT_SECONDS", 0.05)


class FakeStateSet:
    """Orchestration 의 `STATE_SET` 자리. `FakeGame` 이 지금 보이는 객체를 snapshot 으로 낸다."""

    def __init__(self, fake: FakeGame, awaiting: int = 0, down: bool = False) -> None:
        self.fake = fake
        self.awaiting = awaiting
        self.down = down
        self.asked: list[tuple[int, dict]] = []

    async def __call__(self, qa_try_id: int, spec: dict) -> StateSnapshot:
        self.asked.append((qa_try_id, spec))
        if self.down:
            raise StateSetUnavailable("STATE_SET read failed: connection refused")
        version = len(self.asked)
        if self.awaiting > 0:
            self.awaiting -= 1
            return StateSnapshot.model_validate({"stateVersion": version, "sync": "AWAITING_WHOLE", "streamId": "s2"})
        entities = [
            {"entityId": f"s1:{index}", "scope": f"scene:{self.fake.scene}#1", "path": selector, "selector": selector,
             "active": True, "status": "KNOWN",
             "fragments": {"text": {"status": "KNOWN", "value": body["text"]}} if body.get("text") else {}}
            for index, (selector, body) in enumerate(self.fake.objects.items())
        ]
        return StateSnapshot.model_validate(
            {"stateVersion": version, "sync": "SYNCED", "scene": self.fake.scene, "entities": entities}
        )


def _play(fake: FakeGame, jev: FakeJev, state: FakeStateSet) -> StepPlay:
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2)
    assert spec.state_source == "state_set"

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        executer = Executer(fake.channel, spec, JevClient(post=jev), state_reader=state)
        return await executer.play(1, "Press Start.", macros)

    return asyncio.run(go())


def test_jev_picks_from_the_state_set_and_the_turn_names_its_version() -> None:
    fake = _menu()
    jev, state = FakeJev(picks=("press_start",), goal=(0.9,)), FakeStateSet(fake)

    play = _play(fake, jev, state)

    assert play.stopped == "done"
    picked = jev.asked("choice")[0]["state"]
    assert picked == {"scene": "Menu", "objects": [{"path": "Root/Start", "text": "Start", "members": {}}]}
    assert state.asked[0] == (7, EXECUTER_SPEC)
    assert play.turns[0]["state_version"] == 1


def test_jev_is_not_asked_until_the_state_set_is_synced() -> None:
    fake = _menu()
    state = FakeStateSet(fake, awaiting=2)
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    play = _play(fake, jev, state)

    assert play.stopped == "done"
    assert len(state.asked) >= 3
    assert play.turns[0]["state_version"] == 3


def test_play_stops_when_the_state_set_never_syncs() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    play = _play(fake, jev, FakeStateSet(fake, awaiting=10_000))

    assert play.stopped.startswith("STATE-SET-ERROR: STATE_SET not synced")
    assert jev.bodies == []
    assert "state_error" in play.turns[-1]


def test_play_stops_when_the_state_set_cannot_be_read_and_does_not_fall_back() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    play = _play(fake, jev, FakeStateSet(fake, down=True))

    assert play.stopped == "STATE-SET-ERROR: STATE_SET read failed: connection refused"
    assert jev.bodies == []
