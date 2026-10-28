"""The editor rewrites a step's macros from the evidence the executer hands it (ARTEL-1288).

Everything runs over `FakeGame` and a fake model (`Asked`); nothing reaches the network. The
executer side — when the editor is called and how play goes on — is in `test_planner_executer_steps`.
"""

import asyncio

from app.agents.editor import MacroEditor
from app.agents.planner_executer.macros import RegisteredMacros, StepMacros, parse_step
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.qa.jev import JevClient
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, STUCK, Asked, FakeJev, _config, _judge, _menu, _scenario

TURNS = [
    {"step": 1, "turn": 3, "scene": "Menu", "picked": "press_start", "outcome": "passed", "sent": []},
    {"step": 1, "turn": 4, "scene": "Menu", "picked": "press_start", "outcome": "passed", "sent": []},
]


def _repair(fake: FakeGame, asked: Asked, action: str = "press_start", source: str = STUCK, **knobs):
    config = _config(**knobs)

    async def go():
        fake.push_reading()
        registered = RegisteredMacros(fake.channel)
        macros = await parse_step(source, registered)
        editor = MacroEditor(config, fake.channel, config.arch.planner, registered, ask=asked)
        return await editor.repair(_scenario(), 1, macros, action, TURNS)

    return asyncio.run(go())


def _decisions(fake: FakeGame, kind: str) -> list[dict]:
    return [f["payload"] for f in fake.frames("LOG") if f["payload"].get("kind") == kind]


def test_the_editor_reads_the_step_the_source_the_evidence_and_the_screen_and_answers_a_source() -> None:
    fake = _menu()
    asked = Asked(MACROS)

    repaired = _repair(fake, asked)

    assert repaired.refusal is None and list(repaired.macros.actions) == ["press_start", "look_around"]
    ((system, user),) = asked.calls
    assert system.startswith("You fix the macros of ONE test step of a game while it is being played.")
    assert "wait_until(" in system  # the macro grammar is in the system text, as the planner reads it
    assert "The step:\n" in user and '"do": "Press Start."' in user
    assert "The step's macros as they run now:\n```python\n" + STUCK.rstrip() in user
    assert "The action that does not progress the step: `press_start`" in user
    assert "Its docstring: 'Press Start on the title screen." in user
    assert "turn 3 on Menu: passed; no input was sent to the game" in user
    assert "Current screen:\n" in user and "Root/Start" in user
    (fix,) = _decisions(fake, "macro_fix")
    assert fix["decision"]["action"] == "press_start"
    assert fix["decision"]["actions_before"] == fix["decision"]["actions"] == ["press_start", "look_around"]
    assert fix["decision"]["evidence"][0].startswith("  turn 3 on Menu")
    assert fix["message"].startswith("Step 1: the macros were rewritten around `press_start`")


def test_a_refused_rewrite_goes_back_with_the_refusal_and_is_refused_to_the_end() -> None:
    fake = _menu()
    no_goal = MACROS.replace("def done() -> bool:", "def finished() -> bool:")
    asked = Asked(no_goal, no_goal)

    repaired = _repair(fake, asked, max_rewrites=1)

    assert repaired.macros is None and "def done() -> bool:" in repaired.refusal
    assert "The macro parser refused it:" in asked.calls[1][1]
    assert len(_decisions(fake, "macro_fix_refused")) == 2 and _decisions(fake, "macro_fix") == []


def test_a_failing_developer_action_is_taken_out_and_the_others_stay() -> None:
    fake = _menu()
    asked = Asked(MACROS)
    config = _config()

    async def go():
        fake.push_reading()
        registered = RegisteredMacros(fake.channel)
        parsed = await parse_step(STUCK, registered)
        tap, wait = parsed.actions["press_start"], parsed.actions["look_around"]
        macros = StepMacros(
            STUCK,
            {**parsed.actions, "developer:tap_start": tap, "developer:wait_for_scene": wait},
            parsed.probe,
            {"developer:tap_start": "Tap Start.", "developer:wait_for_scene": "Wait for the scene."},
        )
        editor = MacroEditor(config, fake.channel, config.arch.planner, registered, ask=asked)
        return await editor.repair(_scenario(), 1, macros, "developer:tap_start", TURNS)

    repaired = asyncio.run(go())

    assert list(repaired.macros.actions) == ["press_start", "look_around", "developer:wait_for_scene"]
    assert repaired.macros.description("developer:wait_for_scene") == "Wait for the scene."
    user = asked.calls[0][1]
    assert "`developer:tap_start` — a developer macro selected for the step" in user
    assert "Its docstring: 'Tap Start.'" in user


def _run(fake: FakeGame, asked: Asked, judge, jev: FakeJev, **knobs):
    config = _config(no_action_grace_seconds=0.2, **knobs)

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(config, ask=asked, judge=judge, jev=JevClient(post=jev))
        return await runner.run_with_deadline(fake.channel, _scenario())

    return asyncio.run(go())


def test_the_runner_hands_each_steps_play_the_editor() -> None:
    fake = _menu()
    asked = Asked(STUCK, MACROS)  # the planner's answer, then the editor's
    judge, seen = _judge({"passed": True, "reason": "The map opened."})

    state, failure = _run(fake, asked, judge, FakeJev(), repair_after=2, analysis="off")

    assert failure is None and state.step_results[0].passed
    assert asked.calls[1][0].startswith("You fix the macros of ONE test step")
    assert len(_decisions(fake, "macro_fix")) == 1
    # 판정 prompt 는 고침을 turn 줄로 읽는다.
    assert "the editor rewrote the macros around press_start" in seen[0][1]


def test_with_the_editor_off_idle_turns_stop_play_without_a_model_call() -> None:
    fake = _menu()
    asked = Asked(STUCK)
    judge, seen = _judge({"passed": False, "reason": "Nothing happened."})

    _run(fake, asked, judge, FakeJev(), editor="off", max_idle_turns_in_a_row=3, analysis="off")

    assert len(asked.calls) == 1
    assert "Why play stopped: no progress: 3 actions in a row" in seen[0][1]
