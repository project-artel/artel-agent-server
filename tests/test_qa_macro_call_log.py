"""`MacroCallLog`: `run_macro` 호출, `scene` visit, step 별 손 조작 수 (ARTEL-1026)."""

from app.agents.qa.macro.call_log import MacroCallLog


def _call(log: MacroCallLog, step: int, name: str, scene: str, after: str = "") -> None:
    log.record_call(
        step=step,
        name=name,
        arguments={},
        scene=scene,
        scene_after=after or scene,
        passed=True,
        paused=False,
    )


def test_calls_on_one_scene_share_a_visit() -> None:
    log = MacroCallLog()
    _call(log, 3, "cast_pair", "Battle")
    _call(log, 3, "end_turn", "Battle")

    assert [call.visit for call in log.calls] == [1, 1]
    assert [call.name for call in log.calls_in_visit(1)] == ["cast_pair", "end_turn"]


def test_leaving_and_returning_to_a_scene_starts_a_new_visit() -> None:
    log = MacroCallLog()
    _call(log, 3, "cast_pair", "Battle", after="Reward")
    log.record_hand_action(4, "Reward", "Battle")
    _call(log, 5, "cast_pair", "Battle")

    assert [call.visit for call in log.calls] == [1, 3]
    assert log.visit_scene == "Battle"


def test_an_unknown_scene_does_not_start_a_visit() -> None:
    log = MacroCallLog()
    _call(log, 1, "cast_pair", "Battle")
    _call(log, 1, "end_turn", "")

    assert [call.visit for call in log.calls] == [1, 1]


def test_hand_actions_are_counted_per_step() -> None:
    log = MacroCallLog()

    assert log.record_hand_action(2, "Battle", "Battle") == 1
    assert log.record_hand_action(2, "Battle", "Battle") == 2
    assert log.record_hand_action(3, "Battle", "Battle") == 1
    # step 을 모르면 세지 않는다.
    assert log.record_hand_action(None, "Battle", "Battle") == 0
    assert log.hand_actions == {2: 2, 3: 1}


def test_calls_in_step() -> None:
    log = MacroCallLog()
    _call(log, 2, "cast_pair", "Battle")
    _call(log, 3, "end_turn", "Battle")

    assert [call.name for call in log.calls_in_step(3)] == ["end_turn"]
