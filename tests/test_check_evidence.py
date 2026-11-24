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
    # 아무것도 이름 대지 않은 블록은 블록이 없는 것과 같다(ARTEL-1453). 거절하면 planner 가 step 을 통째로 다시 썼다.
    for empty in ("{}", '{"paths": []}', '{"paths": [], "members": []}'):
        assert read_check(empty) is None
    for bad in ('{"paths": "Card"}', '{"values": ["x"]}', "{nope"):
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


def test_pulse_memory_observations_reach_the_evaluator_without_state_set_values() -> None:
    play = _play_checked(_menu(), StepCheck(paths=("Root/Start",)), state_source="pulse_memory")

    assert play.evidence == []
    assert play.state_source == "pulse_memory"
    assert "Root/Start" in play.start_screen and "Root/Stage" in play.end_screen
    drawn = evidence_text(play)
    assert f"Screen before play began:\n{play.start_screen}" in drawn
    assert f"Screen when play stopped:\n{play.end_screen}" in drawn
    assert "(no reading succeeded)" not in drawn


def test_missing_pulse_memory_observations_are_reported_at_both_boundaries() -> None:
    play = StepPlay("done", state_source="pulse_memory")

    drawn = evidence_text(play)

    assert "Screen before play began:\n(no reading available)" in drawn
    assert "Screen when play stopped:\n(no reading available)" in drawn


def test_state_set_without_successful_readings_still_reports_missing_evidence() -> None:
    play = StepPlay("done", check=StepCheck(paths=("Root/Start",)), state_source="state_set")

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
    assert picked["view"] == {"paths": None, "members": None}
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

    # check 가 없어도 활성 씬과 화면 글자(pe9)는 읽는다. 그 밖의 값은 없다.
    assert all("(active scene)" in entry["values"] for entry in play.evidence)
    assert all(name == "(active scene)" or name.endswith(" text") for entry in play.evidence for name in entry["values"])
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


def test_evidence_log_limits_changes_and_records_omissions_without_mutating_observations() -> None:
    import json
    from app.agents.planner_executer.evidence import MAX_EVIDENCE_LOG_BYTES, evidence_log

    changes = [f"적[{i}] 등장" * 20 for i in range(1000)]
    log = evidence_log([], [], changes)
    assert len(json.dumps(log, ensure_ascii=False).encode()) <= MAX_EVIDENCE_LOG_BYTES
    assert 0 < len(log["changes"]) < len(changes)
    assert len(log["changes"]) + log["changes_left_out"] == len(changes) == 1000

    log = evidence_log([{"at": "end", "error": "오류" * 10000}], ["이름" * 10000], [])
    assert len(json.dumps(log, ensure_ascii=False).encode()) <= MAX_EVIDENCE_LOG_BYTES
    assert log["failed_left_out"] == log["unknown_members_left_out"] == 1


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
    log = evidence_log(read, [], [])
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


def test_every_reading_also_keeps_the_words_on_screen_whatever_the_check_named() -> None:
    from app.qa.state_set import visible_texts

    def entity(path: str, text: str | None, scene: str = "StoryScene", active: bool = True) -> dict:
        fragments = {"text": {"status": "KNOWN", "value": text}} if text is not None else {}
        return {"entity_id": path, "scope": scene, "path": path, "selector": path, "active": active, "status": "KNOWN", "fragments": fragments}

    snapshot = StateSnapshot.model_validate({
        "state_version": 3, "sync": "SYNCED", "scene": "StoryScene",
        "entities": [
            entity("Canvas/ChatText", "안녕"),
            entity("Canvas/Hidden", "숨은 글자", active=False),
            entity("Canvas/Empty", ""),
            entity("Canvas/Frame", None),
            entity("Title/Label", "시작하기", scene="TitleScene"),
        ],
    })

    texts = visible_texts(snapshot)

    assert texts == {"Canvas/ChatText text": "안녕"}, "지금 씬에서 켜진 객체의 글자만"
    # 개수 상한이 없다(pe11): 상한에 걸려 빠진 이름은 근거 로그에서 사라진 것(`(absent)`)으로 읽혔다.
    many = StateSnapshot.model_validate({
        "state_version": 3, "sync": "SYNCED", "scene": "StoryScene",
        "entities": [entity(f"Canvas/Line{index:02d}", f"line {index}") for index in range(40)],
    })
    assert len(visible_texts(many)) == 40


def test_the_words_of_objects_that_outlive_scenes_are_read_too() -> None:
    """로컬 try 27 step 9: 튜토리얼 안내가 `@persistent` 아래에 있어 화면에 뜬 글자가 근거에 없었다."""
    from app.qa.state_set import visible_texts

    def entity(path: str, text: str, scope: str, active: bool = True) -> dict:
        return {"entity_id": path, "scope": scope, "path": path, "selector": path, "active": active, "status": "KNOWN",
                "fragments": {"text": {"status": "KNOWN", "value": text}}}

    snapshot = StateSnapshot.model_validate({
        "state_version": 3, "sync": "SYNCED", "scene": "Battle",
        "entities": [
            entity("@persistent/Tutorial/Hint", "적을 클릭하세요.", "persistent"),
            entity("@persistent/Settings/Title", "설정", "persistent", active=False),
            entity("Canvas/Hp", "100", "Battle"),
        ],
    })

    assert visible_texts(snapshot) == {"@persistent/Tutorial/Hint text": "적을 클릭하세요.", "Canvas/Hp text": "100"}


def test_objects_that_come_or_go_switched_off_are_not_listed_as_changes() -> None:
    """로컬 try 27 step 10: 변화 40줄 중 34줄이 새 씬에서 다시 만들어진, 꺼져 있는 설정 메뉴 창의 `appeared` 였다."""
    from app.qa.state_set import state_changes

    start = _snapshot([_enemy("1", "Enemy[0]"), {**_enemy("2", "Menu[0]"), "active": False}])
    end = _snapshot([_enemy("1", "Enemy[0]"), {**_enemy("3", "Menu[1]"), "active": False}, _enemy("4", "Reward[0]")])

    lines, _left_out = state_changes(start, end)

    assert lines == ["Reward[0]: appeared"]


def test_objects_switched_on_or_off_between_two_readings_are_found_unless_the_scene_changed() -> None:
    from app.qa.state_set import switched_between

    before = _snapshot([_enemy("1", "Overlay[0]") | {"active": False}, _enemy("2", "Enemy[0]")])
    after = _snapshot([_enemy("1", "Overlay[0]"), _enemy("2", "Enemy[0]"), _enemy("3", "Card[0]"), _enemy("4", "Off[0]") | {"active": False}])

    assert switched_between(before, after) == {"1": ("Overlay[0]", False), "3": ("Card[0]", None)}
    other = after.model_copy(update={"scene": "Elsewhere"})
    assert switched_between(before, other) == {}


def test_each_reading_keeps_whether_an_object_that_switched_is_on() -> None:
    """시작과 끝만 비교하면 play 안에서 켜졌다 꺼진 것을 못 본다(로컬 try 27 step 10 의 튜토리얼 강조 오버레이). 바뀐 객체는
    직전 reading 과 그 뒤 reading 마다 켜짐이 적혀, 근거 로그가 켜진 구간을 보인다."""
    from app.agents.planner_executer.evidence import evidence_log
    from app.agents.planner_executer.executer import Executer

    off = _snapshot([_enemy("1", "Overlay[0]") | {"active": False}])
    on = _snapshot([_enemy("1", "Overlay[0]")])
    executer = Executer(_menu().channel, PlannerExecuterArchSpec(), None)
    readings = []
    for snapshot in (off, off, on, off):
        values: dict = {}
        executer._note_switching(snapshot, values)
        readings.append({"at": f"turn {len(readings)}", "values": values})

    series = evidence_log(readings, [])["series"]["Overlay[0]"]
    assert series == [["turn 0", "(absent)"], ["turn 1", "(inactive)"], ["turn 2", "(present)"], ["turn 3", "(inactive)"]]
