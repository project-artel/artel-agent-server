"""planner 의 ```check 값이 evaluator 의 근거가 된다.

planner 가 step 마다 판정에 필요한 값을 ```check 로 정하고, executer 가 그 값을 play 시작·매 turn 시작·끝에 `STATE_SET` 에서
읽어 `StepPlay.evidence` 에 남기며, evaluator 는 화면 대신 그 값이 언제 무엇에서 무엇으로 바뀌었는지를 읽는다.
"""

import asyncio
import dataclasses
import json

import pytest

from app.agents.evaluator.verdict import Evaluator, evidence_text, request
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.executer import Executer, StepPlay
from app.agents.planner_executer.macros import RegisteredMacros, StepCheck, StepObserve, parse_step
from app.agents.planner_executer.runner import repair_brief
from app.agents.planner_executer.planner import (
    read_check,
    split_check_block,
    split_observe_block,
)
from app.agents.qa.macro.errors import MacroRejection
from app.qa.jev import JevClient
from app.qa.state_set import StateSnapshot, StateView, evidence_values, unmatched_paths
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import (
    MACROS,
    FakeJev,
    _config,
    _judge,
    _menu,
    _scenario,
)
from tests.test_state_set import BATTLE, FakeStateSet, _quick  # noqa: F401 - the fixture makes play quick


def test_the_check_block_is_read_beside_observe_and_an_empty_one_is_refused() -> None:
    answer = (
        '```python\npass\n```\n```observe\n{"paths": ["Card(Clone)"]}\n```\n'
        '```check\n{"paths": ["Root/Stage"], "members": ["Player.Hp"]}\n```'
    )
    rest, observe_text = split_observe_block(answer)
    rest, check_text = split_check_block(rest)

    assert "observe" not in rest and "check" not in rest and observe_text is not None
    assert read_check(check_text) == StepCheck(paths=("Root/Stage",), members=("Player.Hp",))
    assert read_check(None) is None
    assert read_check('{"paths": [], "members": ["Player.Hp"]}') == StepCheck(paths=None, members=("Player.Hp",))
    for bad in ("{}", '{"paths": []}', '{"paths": "Card"}', '{"values": ["x"]}', "{nope"):
        with pytest.raises(MacroRejection):
            read_check(bad)


def test_named_members_keep_only_the_objects_that_carry_them_and_statics() -> None:
    view = StateView(members=("MagicEnemy.Hp", "CombineZone.Instance.IsAwaitingTarget", "Ghost.thing"))

    reading = evidence_values(BATTLE, view)
    values, unknown = reading.values, reading.unknown_members

    assert values["static CombineZone.Instance.IsAwaitingTarget"] is False
    enemies = [name for name in values if name.endswith(" MagicEnemy.Hp")]
    assert enemies and all(values[name.removesuffix(" MagicEnemy.Hp")] == "(present)" for name in enemies)
    # 그 멤버가 없는 live 객체는 근거가 아니다. 튜토리얼·버튼이 전부 실리면 근거가 묻힌다.
    assert not any(name.startswith(("TutorialController", "DebugCanvas")) for name in values)
    assert unknown == ["Ghost.thing"]


def _play_checked(fake: FakeGame, check: StepCheck | None, **knobs) -> StepPlay:
    spec = PlannerExecuterArchSpec(**{"no_action_grace_seconds": 0.2, **knobs})

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        macros = dataclasses.replace(macros, check=check)
        executer = Executer(fake.channel, spec, JevClient(post=FakeJev()), state_reader=FakeStateSet(fake))
        return await executer.play(1, "Press Start.", macros)

    return asyncio.run(go())


def test_the_check_values_are_read_at_the_start_before_each_turn_and_at_the_end() -> None:
    play = _play_checked(_menu(), StepCheck(paths=("Root/Start", "Root/Stage")))

    assert play.stopped == "done"
    labels = [entry["at"] for entry in play.evidence]
    assert labels[0] == "start" and labels[-1] == "end" and "turn 1" in labels
    assert play.evidence[0]["values"] == {"(active scene)": "Menu", "Root/Start": "(present)", "Root/Start text": "Start"}
    assert play.evidence[-1]["values"] == {"(active scene)": "Map", "Root/Stage": "(present)", "Root/Stage text": "Stage 0"}

    drawn = evidence_text(play)
    assert drawn.startswith("Game values (the plan named paths Root/Start, Root/Stage; the active scene is always read)")
    assert '  (active scene): start: "Menu" → turn 1: "Map" → end: "Map"' in drawn
    assert '  Root/Start text: start: "Start" → turn 1: (absent) → end: (absent)' in drawn
    assert '  Root/Stage text: start: (absent) → turn 1: "Stage 0" → end: "Stage 0"' in drawn


def test_a_value_that_held_still_says_so_and_a_failed_reading_is_named() -> None:
    read = [
        {"at": "start", "state_version": 1, "values": {"Gold.amount": 100}},
        {"at": "turn 1", "error": "STATE_SET read failed: timeout"},
        {"at": "turn 2", "state_version": 3, "values": {"Gold.amount": 100}},
        {"at": "end", "state_version": 4, "values": {"Gold.amount": 100}},
    ]
    play = StepPlay("done", check=StepCheck(members=("Gold.amount",)), evidence=read, evidence_unknown=["Ghost.x"])

    drawn = evidence_text(play)

    assert "  Gold.amount: 100 (unchanged from start to end)" in drawn
    assert "readings that failed: turn 1: STATE_SET read failed: timeout" in drawn
    assert "not by itself a sign the result is missing: Ghost.x" in drawn


def test_without_a_state_set_nothing_is_read_and_the_evaluator_is_told_so() -> None:
    play = _play_checked(_menu(), StepCheck(paths=("Root/Start",)), state_source="pulse_memory")

    assert play.evidence == []
    assert "(no reading succeeded)" in evidence_text(play)


def test_the_evaluator_reads_the_values_and_answers_four_things() -> None:
    play = _play_checked(_menu(), StepCheck(paths=("Root/Stage",)))
    answer = {
        "passed": True,
        "reason": "지도 씬에 Stage 0 이 나타났다.",
        "state_detail": 'Root/Stage text: (absent) → turn 1: "Stage 0"',
        "insight": "",
    }
    judge, seen = _judge(answer)

    verdict = asyncio.run(Evaluator(_config(), judge).judge(_scenario(), 1, play))

    assert verdict.passed and verdict.message == answer["reason"]
    assert verdict.details() == {"state_detail": answer["state_detail"], "insight": "", "evidence_sufficient": True}
    system, user = seen[0]
    assert user == request(_scenario(), 1, play)
    assert "Root/Stage text: start: (absent) → turn 1" in user and "Screen when" not in user
    # v12 부터 evaluator 는 `insight` 대신 게임에 대한 평가 `game_insight` 를 쓴다.
    assert '"state_detail"' in system and '"game_insight"' in system and '"insight"' not in system


def test_each_turn_log_carries_the_state_jev_read_and_its_view() -> None:
    fake = _menu()
    _play_checked(fake, None)

    turns = [frame["payload"]["turn"] for frame in fake.frames("LOG") if frame["payload"].get("kind") == "turn"]
    picked = next(turn for turn in turns if turn.get("picked"))
    assert picked["view"] == {"paths": None, "members": None, "level": 0}
    assert picked["jev_state"]["objects"][0]["path"] == "Root/Start"


def test_a_jev_state_too_large_for_one_frame_is_clipped_not_the_turn() -> None:
    from app.agents.planner_executer.executer import MAX_TURN_LOG_BYTES, _turn_log

    record = {"step": 1, "turn": 3, "done": False, "picked": "a", "outcome": "passed",
              "jev_state": {"objects": [{"path": "x" * 1000}] * 100}}
    body = _turn_log(record).turn

    assert body["picked"] == "a" and body["jev_state_clipped"] is True and "jev_state" not in body
    assert len(json.dumps(body, ensure_ascii=False).encode()) <= MAX_TURN_LOG_BYTES


def test_the_active_scene_is_read_even_when_the_named_values_match_nothing() -> None:
    """런 163 step 3: planner 가 없는 멤버 이름만 check 에 적어 기록이 비었고, 전투 씬에 들어갔는데도 근거 부족으로 실패했다."""
    play = _play_checked(_menu(), StepCheck(members=("MapMove.position",)))

    drawn = evidence_text(play)

    assert '  (active scene): start: "Menu" → turn 1: "Map" → end: "Map"' in drawn
    assert "names the plan listed that matched nothing in any reading" in drawn and "MapMove.position" in drawn


def test_a_check_path_that_never_matched_is_named_and_one_that_came_later_is_not() -> None:
    """지금 없는 경로를 거절하지 않는다: 다음 씬에 생기는 객체가 있다. 끝까지 한 번도 맞지 않은 경로만 남는다(묶음 3)."""
    play = _play_checked(_menu(), StepCheck(paths=("Root/Stage", "Root/Nowhere")))

    assert play.evidence_unknown == ["path Root/Nowhere"]
    assert "path Root/Nowhere" in evidence_text(play)


def test_an_observe_path_that_never_matched_reaches_the_planner_repair() -> None:
    fake = _menu()
    spec = PlannerExecuterArchSpec(no_action_grace_seconds=0.2)

    async def go() -> StepPlay:
        fake.push_reading()
        macros = await parse_step(MACROS, RegisteredMacros(fake.channel))
        macros = dataclasses.replace(macros, observe=StepObserve(paths=("Root/Start", "Root/Ghost")))
        executer = Executer(fake.channel, spec, JevClient(post=FakeJev()), state_reader=FakeStateSet(fake))
        return await executer.play(1, "Press Start.", macros)

    play = asyncio.run(go())

    assert play.observe_unknown == ["Root/Ghost"]
    reason, _ = repair_brief(dataclasses.replace(play, repair_request={"action": None, "reason": "stalled"}))
    assert "Paths in your ```observe block that matched no object while the step played" in reason
    assert reason.count("Root/Ghost") == 1


def test_unmatched_paths_count_inactive_and_stale_objects_as_matched() -> None:
    present = [entity.path or entity.selector for entity in BATTLE.entities][:1]
    assert unmatched_paths(BATTLE, present + ["Nope/Thing"]) == ["Nope/Thing"]
    assert unmatched_paths(BATTLE, None) == []


def test_without_a_check_block_the_active_scene_is_still_read() -> None:
    play = _play_checked(_menu(), None)

    assert all(entry["values"].keys() == {"(active scene)"} for entry in play.evidence)
    assert "the plan named no values (no ```check block)" in evidence_text(play)


def _snapshot(entities: list[dict], omitted: int = 0) -> StateSnapshot:
    return StateSnapshot.model_validate(
        {"stateVersion": 1, "sync": "SYNCED", "scene": "Battle", "entities": entities, "omittedEntities": omitted}
    )


def _enemy(entity_id: str, path: str, **overrides) -> dict:
    return {
        "entityId": entity_id, "scope": "scene:Battle#1", "path": path, "selector": path, "active": True,
        "status": "KNOWN", "members": {"Battle.Enemy::hp#0": {"status": "KNOWN", "value": 10}}, **overrides,
    }


def test_inactive_stale_and_unread_are_kept_as_what_state_set_says() -> None:
    snapshot = _snapshot([
        _enemy("1", "Enemy[0]"),
        _enemy("2", "Enemy[1]", active=False),
        _enemy("3", "Enemy[2]", status="STALE"),
        _enemy("4", "Enemy[3]", members={"Battle.Enemy::hp#0": {"status": "UNREAD", "reason": "getter refused"}}),
    ])

    reading = evidence_values(snapshot, StateView(paths=("Enemy",)))

    assert reading.values["Enemy[0]"] == "(present)" and reading.values["Enemy[0] Enemy.hp"] == 10
    assert reading.values["Enemy[1]"] == "(inactive)"
    assert reading.values["Enemy[2]"] == "(stale)"
    assert reading.values["Enemy[3] Enemy.hp"] == "(unread: getter refused)"
    assert reading.complete


def test_a_name_missing_from_an_incomplete_reading_is_not_read_not_absent() -> None:
    from app.agents.planner_executer.evidence import evidence_log, points_of

    read = [
        {"at": "start", "values": {"Enemy[0]": "(present)", "Enemy[0] Enemy.hp": 10}},
        {"at": "turn 1", "values": {}, "clipped": 3},
        {"at": "turn 2", "values": {"Enemy[0]": "(present)", "Enemy[0] Enemy.hp": 4}},
        {"at": "end", "values": {}},
    ]

    assert points_of("Enemy[0] Enemy.hp", read) == [("start", "10"), ("turn 1", "(not read)"), ("turn 2", "4"), ("end", "(absent)")]
    play = StepPlay("done", check=StepCheck(paths=("Enemy",)), evidence=read)
    drawn = evidence_text(play)
    assert "readings that could not cover every object" in drawn and "turn 1" in drawn
    log = evidence_log(read, [])
    assert log["series"]["Enemy[0] Enemy.hp"][1] == ["turn 1", "(not read)"]
    assert log["incomplete"] == [{"at": "turn 1", "clipped": 3, "omitted": 0}]


def test_a_reading_past_the_size_limit_counts_what_it_left_out() -> None:
    snapshot = _snapshot([_enemy(str(n), f"Enemy[{n}]") for n in range(5)], omitted=2)

    reading = evidence_values(snapshot, StateView(paths=("Enemy",), limit=120))

    assert reading.clipped > 0 and reading.omitted == 2 and not reading.complete


def test_each_play_leaves_its_evidence_in_the_log() -> None:
    fake = _menu()
    _play_checked(fake, StepCheck(paths=("Root/Stage",)))

    (log,) = [frame["payload"] for frame in fake.frames("LOG") if frame["payload"].get("kind") == "evidence"]
    assert log["step"] == 1 and log["evidence"]["readings"] >= 2
    assert log["evidence"]["series"]["(active scene)"][0] == ["start", '"Menu"']


def test_a_scene_prefixed_persistent_path_is_read_as_the_selector() -> None:
    """런 166 step 4: 화면의 "switched off" 목록이 `DontDestroyOnLoad/@persistent/…` 로 적어, planner 가 그 꼴로 check 와
    `done()` 을 썼고 아무것도 찾지 못했다."""
    from app.agents.planner_executer.planner import normalize_paths

    tutorial = {
        "entityId": "9", "scope": "persistent", "path": "TutorialController", "selector": "@persistent/TutorialController[6]",
        "active": True, "status": "KNOWN", "members": {"Tutorial.TutorialController::finished#0": {"status": "KNOWN", "value": False}},
    }
    reading = evidence_values(_snapshot([tutorial]), StateView(paths=("DontDestroyOnLoad/@persistent/TutorialController[6]",)))

    assert reading.values["@persistent/TutorialController[6] TutorialController.finished"] is False
    source = 'def done() -> bool:\n    return member(selector("DontDestroyOnLoad/@persistent/TutorialController[6]"), "X.y")\n'
    assert 'selector("@persistent/TutorialController[6]")' in normalize_paths(source)


def test_what_changed_outside_the_plan_is_listed_for_the_evaluator() -> None:
    from app.qa.state_set import state_changes

    start = _snapshot([_enemy("1", "Enemy[0]"), _enemy("2", "Enemy[1]")])
    end = _snapshot([
        _enemy("1", "Enemy[0]", members={"Battle.Enemy::hp#0": {"status": "KNOWN", "value": 4}}),
        _enemy("3", "Reward[0]"),
    ])

    lines, left_out = state_changes(start, end)

    assert lines == ["Enemy[0] Enemy.hp: 10 → 4", "Reward[0]: appeared", "Enemy[1]: gone"] and left_out == 0
    play = StepPlay("done", check=StepCheck(paths=("Nothing",)), evidence=[{"at": "start", "values": {}}], changes=lines)
    assert "Everything else in the game that changed" in evidence_text(play) and "Enemy[1]: gone" in evidence_text(play)


def test_a_play_records_what_changed_and_each_turn_names_its_frame() -> None:
    fake = _menu()
    play = _play_checked(fake, StepCheck(paths=("Root/Stage",)))

    assert "Root/Stage: appeared" in play.changes and "Root/Start: gone" in play.changes
    assert all("frame" in turn for turn in play.turns)
    (log,) = [frame["payload"] for frame in fake.frames("LOG") if frame["payload"].get("kind") == "evidence"]
    assert "Root/Stage: appeared" in log["evidence"]["changes"]
