"""Executer 가 Jev 에게 보내는 state 를 Orchestration 의 `STATE_SET` 에서 읽는다 (ARTEL-1297).

대조 테스트의 fixture 둘은 같은 `pulse` 열에서 왔다. `wordventure-try205-pulses.jsonl.gz` 는 로컬 런 155(try 205)의
`SDK_TO_ORCHE` `pulse` 853개 원문이고, `wordventure-try205-state-set-checkpoints.jsonl.gz` 는 orchestration-server
의 reducer(ARTEL-1296, `StateSet`)가 그 열을 적용하다가 다섯 지점에서 `EXECUTER_SPEC` 으로 낸 snapshot 이다. 같은
지점까지 `PulseMemory` 에 적용해 그린 state 와 같아야 한다 — 출처만 바뀌고 Jev 가 읽는 글은 같다는 것이 이 변경의
전제다.
"""

import asyncio
import dataclasses
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


# ── C: planner 가 정하는 범위, EXPAND_STATE, REPLAN (ARTEL-1295) ─────────────────


from app.agents.planner_executer.executer import EXPAND_STATE, REPLAN  # noqa: E402
from app.agents.planner_executer.macros import StepObserve  # noqa: E402
from app.agents.planner_executer.planner import read_observe, split_observe_block  # noqa: E402
from app.agents.qa.macro.errors import MacroRejection  # noqa: E402
from app.qa.state_set import StateView  # noqa: E402

BATTLE = StateSnapshot.model_validate(CHECKPOINTS[-1]["snapshot"])


def test_the_observe_block_is_read_and_a_bad_one_is_refused() -> None:
    rest, text = split_observe_block('```python\npass\n```\n```observe\n{"paths": ["Card(Clone)"], "members": ["Card.cardType"]}\n```')
    assert "observe" not in rest
    assert read_observe(text) == StepObserve(paths=("Card(Clone)",), members=("Card.cardType",))
    assert read_observe(None) is None
    assert read_observe('{"members": ["Player.Hp"]}') == StepObserve(paths=None, members=("Player.Hp",))
    for bad in ("[1]", '{"paths": "Card"}', '{"paths": [""]}', '{"scopes": []}', "{nope"):
        with pytest.raises(MacroRejection):
            read_observe(bad)


def test_in_battle_the_whole_state_puts_the_tutorial_first_and_cuts_every_battle_object() -> None:
    """pe9 의 순서(`씬/selector`)로는 DontDestroyOnLoad 가 앞에 서서 전투 객체가 하나도 남지 않는다. 이 변경의 이유다."""
    state, clipped = render_jev_state(BATTLE)
    kept = [entry["path"] for entry in state["objects"]]
    assert clipped == 25
    assert not any(path.startswith(("Card(Clone)", "RangedCat", "Word[", "CombineSystem")) for path in kept)

    # 활성 씬을 앞에 세우면 같은 상한 안에 전투 객체가 들어온다. 그래도 다 들지는 않는다 — 범위를 정하는 것은 planner 다.
    first, clipped_first = render_jev_state(BATTLE, view=StateView(active_scene_first=True))
    kept_first = [entry["path"] for entry in first["objects"]]
    assert clipped_first > 0
    assert any(path.startswith("Card(Clone)") for path in kept_first)
    assert not any(path.startswith("TutorialController") for path in kept_first)


def test_a_planned_view_fits_the_battle_objects_under_the_limit() -> None:
    view = StateView(
        paths=("Card(Clone)", "RangedCat(Clone)", "Word", "CombineSystem/CombineButton", "DebugCanvas/TurnEndButton"),
        members=("Card.cardType", "Card.Name", "MagicEnemy.Hp", "Player.Hp", "CombineButton.activateButton", "Ghost.thing"),
        active_scene_first=True,
    )

    state, clipped = render_jev_state(BATTLE, view=view)
    paths = [entry["path"] for entry in state["objects"]]

    assert clipped == 0
    assert any(path.startswith("Card(Clone)") for path in paths)
    assert any(path.startswith("RangedCat(Clone)") for path in paths)
    assert any(path.startswith("DebugCanvas[14]/TurnEndButton") for path in paths)
    assert not any(path.startswith(("TutorialController", "SettingsMenu", "Shoot")) for path in paths)
    assert "Ghost.thing" in state["unknown_members"]
    enemy = next(entry for entry in state["objects"] if entry["path"] == "RangedCat(Clone)[19]")
    assert set(enemy["members"]) <= {"MagicEnemy.Hp"}


class FakeBigStateSet(FakeStateSet):
    """메뉴 객체에 덧붙여 상한을 넘길 만큼 큰 DontDestroyOnLoad 객체를 낸다."""

    async def __call__(self, qa_try_id: int, spec: dict) -> StateSnapshot:
        snapshot = await super().__call__(qa_try_id, spec)
        filler = [
            {"entityId": f"s1:big{index}", "scope": "persistent", "path": f"Tutorial/Part{index}", "active": True,
             "status": "KNOWN", "fragments": {"text": {"status": "KNOWN", "value": "x" * 70}},
             "members": {f"Tutorial.Step::value{n}#0": {"status": "KNOWN", "value": "y" * 70} for n in range(12)}}
            for index in range(10)
        ]
        extra = StateSnapshot.model_validate({"stateVersion": 0, "sync": "SYNCED", "entities": filler}).entities
        return snapshot.model_copy(update={"entities": [*snapshot.entities, *extra]})


def _play_planned(fake: FakeGame, jev: FakeJev, state, observe: StepObserve | None, can_replan: bool = False, **knobs) -> StepPlay:
    spec = PlannerExecuterArchSpec(**{"no_action_grace_seconds": 0.2, **knobs})

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        macros = dataclasses.replace(macros, observe=observe)
        executer = Executer(fake.channel, spec, JevClient(post=jev), state_reader=state)
        return await executer.play(1, "Press Start.", macros, None, can_replan)

    return asyncio.run(go())


def test_over_the_limit_the_state_says_how_many_objects_it_left_out_and_offers_expand() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    play = _play_planned(fake, jev, FakeBigStateSet(fake), None)

    first = jev.asked("choice")[0]
    assert first["state"]["objects"][0]["path"] == "Root/Start"  # 활성 씬이 먼저다
    assert first["state"]["omitted_objects"] > 0
    assert EXPAND_STATE in first["questions"]["answer"]["criteria"]
    assert REPLAN not in first["questions"]["answer"]["criteria"]
    assert play.turns[0]["omitted_objects"] > 0 and play.stopped == "done"


def test_expand_state_widens_the_view_one_step_at_a_time() -> None:
    fake = _menu()
    jev = FakeJev(picks=(EXPAND_STATE, EXPAND_STATE, "press_start"), goal=(0.9,))
    observe = StepObserve(paths=("Nowhere",), members=("Player.hp",))

    play = _play_planned(fake, jev, FakeStateSet(fake), observe)

    states = [body["state"] for body in jev.asked("choice")]
    assert states[0]["objects"] == []  # 범위 밖이라 아무것도 없다
    assert states[1]["objects"] == [{"path": "Root/Start", "text": "Start", "members": {}}]  # path 를 풀었다
    assert EXPAND_STATE not in jev.asked("choice")[2]["questions"]["answer"]["criteria"]  # 다 넓혔다
    assert [turn.get("expanded_to") for turn in play.turns[:2]] == [1, 2]
    assert play.stopped == "done"


def test_replan_stops_play_for_the_runner_and_is_offered_only_when_allowed() -> None:
    fake = _menu()
    jev = FakeJev(picks=(REPLAN,), goal=(0.9,))

    play = _play_planned(fake, jev, FakeStateSet(fake), None, can_replan=True)

    assert REPLAN in jev.asked("choice")[0]["questions"]["answer"]["criteria"]
    assert play.replan_requested and play.stopped.startswith("REPLAN:")
    assert play.played()["end_state_version"] == 1


def test_the_whole_view_of_pe9_is_kept_with_observation_all() -> None:
    fake = _menu()
    jev = FakeJev(picks=("press_start",), goal=(0.9,))

    _play_planned(fake, jev, FakeBigStateSet(fake), StepObserve(paths=("Nowhere",)), can_replan=True, observation="all")

    first = jev.asked("choice")[0]
    assert "omitted_objects" not in first["state"]
    assert set(first["questions"]["answer"]["criteria"]) == {"press_start", "look_around", "none_fits"}
