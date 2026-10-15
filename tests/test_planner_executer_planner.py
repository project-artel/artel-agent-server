"""The planner writes each step from the rules, the `g` API, the macro grammar, the game map and the
step; every answer is checked, a refused one is sent back with its reasons, and a step whose module
is still refused after the rewrites fails without being played."""

import asyncio
import re

import pytest

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer import g as g_module
from app.agents.planner_executer import sandbox
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.planner import Planner, extract_code
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.arch import QaArchSpec
from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY
from app.qa.jev import JevClient
from app.qa.run_config import resolve_run_config
from app.qa.scene_context import SceneCapability, SceneContext, SceneContextEntry
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_play import START, FakeJev

# v1 이전의 모양: `run` 이 Python coroutine 이다.
PYTHON_RUN = START.replace(
    '''run_start = """
def run_start() -> None:
    start: object = find(name="Start")
    click(start)
    wait_until(scene() == "Map", 1, "Start did not open the map.")
"""
''',
    '''async def run_start(g):
    await g.click(g.find("Start"))
''',
)

# macro 문법이 받지 않는 `and`.
REFUSED_MACRO = START.replace(
    'wait_until(scene() == "Map", 1, "Start did not open the map.")',
    'require(scene() == "Map" and exists(start), "Start did not open the map.")',
)

# `check` 가 `done` 을 빌려 쓴다.
CHECK_BORROWS_DONE = START.replace(
    'await g.expect(lambda: g.scene == "Map", "the map opened", timeout=0.1)',
    'await g.expect(lambda: done(g), "the map opened", timeout=0.1)',
)


class Asked:
    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return f"```python\n{self.answers.pop(0)}```"


@pytest.fixture(autouse=True)
def _quick(monkeypatch) -> None:
    monkeypatch.setattr(g_module, "SETTLE_QUIET_SECONDS", 0.001)
    monkeypatch.setattr(executer_module, "IDLE_LOOK_SECONDS", 0.01)


def _menu() -> FakeGame:
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})

    def to_map(game: FakeGame) -> None:
        game.scene = "Map"
        game.objects = {"Root/Stage": {"text": "Stage 0"}}

    fake.reactions[("press", "Root/Start")] = to_map
    fake.channel.scene.scene_context = SceneContext(
        scenes=[
            SceneContextEntry(
                scene_name="Menu",
                capabilities=[SceneCapability(interaction="click", control_label="Start", summary="starts a game")],
            ),
            SceneContextEntry(scene_name="Map"),
        ]
    )
    return fake


def _scenario() -> QaScenario:
    return QaScenario(
        title="Start",
        steps=[QaStep(action="Press Start.", case_id=5, case=QaCaseRef(id=5, expected="The map opens."))],
    )


def _config(**knobs):
    return resolve_run_config(
        arch=QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec(**knobs))
    )


def _run(fake: FakeGame, asked: Asked, **knobs):
    config = _config(**knobs)

    async def go():
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, ask=asked)
        runner = PlannerExecuterRunner(config, planner, jev=JevClient(post=FakeJev()))
        return await runner.run_with_deadline(fake.channel, _scenario())

    return asyncio.run(go())


def _verdicts(fake: FakeGame) -> list[dict]:
    return [f["payload"] for f in fake.frames("STATUS") if f["payload"].get("step") is not None]


def test_the_planner_writes_the_executer_plays_and_the_evaluator_judges() -> None:
    fake = _menu()
    asked = Asked(START)

    state, failure = _run(fake, asked)

    assert failure is None
    assert [result.passed for result in state.step_results] == [True]
    (verdict,) = _verdicts(fake)
    assert verdict["message"] == "Held: the map opened"
    system, user = asked.calls[0]
    assert system.startswith("You write ONE test step as a set of funcs")
    # 문법 글은 tool loop agent 가 읽는 것과 같은 글이다.
    assert GRAMMAR in system and system.rstrip().endswith(NUMBER_EQUALITY)
    assert '{"<name>"' in system  # doubled braces in the file come out single
    assert "Scenes in this build" in user and '- click "Start" — starts a game' in user
    assert '"verify": "The map opens."' in user
    assert "Current game state (pulse):\nscene 'Menu'" in user


def test_a_python_run_is_sent_back_and_the_rewrite_is_played() -> None:
    fake = _menu()
    asked = Asked(PYTHON_RUN, START)

    _run(fake, asked)

    assert len(asked.calls) == 2
    assert "`async def run_start` — a func's `run` is a macro" in asked.calls[1][1]
    assert _verdicts(fake)[0]["status"] == "COMPLETED"


def test_a_macro_the_parser_refuses_is_sent_back_with_the_refusal() -> None:
    fake = _menu()
    asked = Asked(REFUSED_MACRO, START)

    _run(fake, asked)

    feedback = asked.calls[1][1]
    assert "`run_start`: MACRO_NODE_REJECTED (line 5)" in feedback and "`and`" in feedback
    assert _verdicts(fake)[0]["status"] == "COMPLETED"


def test_a_check_that_uses_another_function_of_the_module_is_sent_back() -> None:
    fake = _menu()
    asked = Asked(CHECK_BORROWS_DONE, START)

    _run(fake, asked)

    assert re.search(
        r"the module is refused: line \d+: `check` uses `done`, a function this module defines\. "
        r"`check` must stand alone",
        asked.calls[1][1],
    )


def test_a_step_still_refused_after_the_rewrites_fails_without_play() -> None:
    fake = _menu()
    asked = Asked(PYTHON_RUN, PYTHON_RUN)

    _run(fake, asked, max_rewrites=1)

    assert len(asked.calls) == 2
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].startswith(
        "PLANNER-REFUSED: the step module was still refused after 1 rewrites: "
    )
    assert fake.actions == []


@pytest.mark.parametrize("borrowed", ["done", "lost", "when_start", "read_stage"])
def test_check_may_not_name_any_top_level_function(borrowed: str) -> None:
    source = (
        "def read_stage(g):\n    return 0\n\ndef lost(g):\n    return None\n"
        + START.replace(
            'await g.expect(lambda: g.scene == "Map", "the map opened", timeout=0.1)',
            f"await g.expect(lambda: {borrowed}(g), 'x')",
        )
    )

    with pytest.raises(sandbox.ModuleRejected, match=f"`check` uses `{borrowed}`"):
        sandbox.check(source)


def test_check_may_hold_its_own_helpers_and_lambdas() -> None:
    standing = START.replace(
        'async def check(g):\n    await g.expect(lambda: g.scene == "Map", "the map opened", timeout=0.1)',
        "async def check(g):\n    def on_map():\n        return g.scene == 'Map'\n"
        "    await g.expect(lambda: on_map(), 'the map opened', timeout=0.1)",
    )

    sandbox.check(standing)


def test_extract_code_takes_the_fenced_block() -> None:
    assert extract_code("Here:\n```python\nx = 1\n```\nthanks") == "x = 1\n"
    assert extract_code("x = 1") == "x = 1\n"
