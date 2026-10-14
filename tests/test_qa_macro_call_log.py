"""`MacroCallLog`: `run_macro` 호출과 step 별 손 조작 수 (ARTEL-1026)."""

from app.agents.qa.macro.call_log import MacroCallLog


def _call(log: MacroCallLog, step: int, name: str) -> None:
    log.record_call(
        step=step,
        name=name,
        arguments={},
        scene="Battle",
        scene_after="Battle",
        passed=True,
        paused=False,
    )


def test_hand_actions_are_counted_per_step() -> None:
    log = MacroCallLog()

    assert log.record_hand_action(2) == 1
    assert log.record_hand_action(2) == 2
    assert log.record_hand_action(3) == 1
    # step 을 모르면 세지 않는다.
    assert log.record_hand_action(None) == 0
    assert log.hand_actions == {2: 2, 3: 1}


def test_calls_in_step() -> None:
    log = MacroCallLog()
    _call(log, 2, "cast_pair")
    _call(log, 3, "end_turn")

    assert [call.name for call in log.calls_in_step(3)] == ["end_turn"]
