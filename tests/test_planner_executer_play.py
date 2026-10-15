"""The executer plays a fixed step module and the evaluator judges it: when play stops, which func
runs, how a func's macro reaches the game and Jev, and the verdict frames that go out."""

import asyncio

import pytest

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer import g as g_module
from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.planner import Written
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.arch import QaArchSpec
from app.qa.jev import JevClient
from app.qa.run_config import resolve_run_config
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame

# Menu 의 Start 를 누르면 Map 으로 간다.
START = '''
def when_start(g):
    return g.scene == "Menu"

run_start = """
def run_start() -> None:
    start: object = find(name="Start")
    click(start)
    wait_until(scene() == "Map", 1, "Start did not open the map.")
"""

FUNCS = {"start": ("Press Start on the title screen", when_start, run_start)}

def done(g):
    return g.scene == "Map"

async def check(g):
    await g.expect(lambda: g.scene == "Map", "the map opened", timeout=0.1)
'''

# Menu 에서 아무것도 하지 않는 func. 눌러도 화면이 그대로다.
LOOK = '''
run_look = """
def run_look() -> None:
    require(scene() == "Menu", "Not on the menu.")
"""
'''


class FixedWriter:
    def __init__(self, *sources: str) -> None:
        self.sources = list(sources)

    async def write(self, scenario, step, g) -> Written:
        return Written(self.sources[step - 1], [])


class FakeJev:
    """`JevClient` 의 `post` 자리. 받은 요청 본문을 남기고 정해 둔 답을 낸다."""

    def __init__(self, *, choice: str | None = None, noul: float | None = None) -> None:
        self.choice, self.noul = choice, noul
        self.bodies: list[dict] = []

    async def __call__(self, body: dict) -> dict:
        self.bodies.append(body)
        if body["questions"]["answer"]["type"] == "choice":
            return {"answers": {"answer": {"choice": self.choice}}}
        return {"answers": {"answer": {"noul": self.noul}}}


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
    return fake


def _scenario(steps: int = 1) -> QaScenario:
    return QaScenario(
        title="S",
        steps=[
            QaStep(action=f"Step {n}.", case_id=11, case=QaCaseRef(id=11)) for n in range(1, steps + 1)
        ],
    )


def _run(fake: FakeGame, *sources: str, jev: JevClient | None = None, **knobs):
    config = resolve_run_config(
        arch=QaArchSpec(
            engine="planner_executer",
            planner=PlannerExecuterArchSpec(no_func_grace_seconds=0.2, **knobs),
        )
    )
    jev = jev or JevClient(post=FakeJev())

    async def go():
        fake.push_reading()
        return await PlannerExecuterRunner(config, FixedWriter(*sources), jev=jev).run_with_deadline(
            fake.channel, _scenario(len(sources))
        )

    return asyncio.run(go())


def _verdicts(fake: FakeGame) -> list[dict]:
    return [f["payload"] for f in fake.frames("STATUS") if f["payload"].get("step") is not None]


def _turns(fake: FakeGame) -> list[dict]:
    return [f["payload"]["turn"] for f in fake.frames("LOG") if f["payload"].get("kind") == "turn"]


# --- a step from play to verdict ------------------------------------------------------------------


def test_a_step_is_played_until_done_and_judged_by_check() -> None:
    fake = _menu()

    state, failure = _run(fake, START)

    assert failure is None
    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "COMPLETED" and verdict["message"] == "Held: the map opened"
    assert verdict["case_id"] == 11 and verdict["is_verification"] is True
    closing = fake.frames("STATUS")[-1]["payload"]
    assert closing["result"] == "PASSED" and closing["summary"]["steps"]["passed"] == 1
    assert state.finished
    (turn,) = _turns(fake)
    assert turn["applicable"] == ["start"] and turn["picked"] == "start"
    assert turn["outcome"] == "passed"
    # 입력은 macro 의 statement 마다 `ACTION` 하나로, func 하나는 `run_macro` 한 쌍으로 남는다.
    (action,) = fake.frames("ACTION")
    assert action["payload"]["message"] == "click(start)"
    assert [f["payload"]["tool"] for f in fake.frames("TOOL")] == ["run_macro"]


def test_a_check_that_does_not_hold_fails_the_step() -> None:
    fake = _menu()
    wrong = START.replace('g.scene == "Map", "the map opened"', 'g.scene == "Shop", "the shop opened"')

    _run(fake, wrong)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"] == "Expected: the shop opened"
    assert fake.frames("STATUS")[-1]["payload"]["result"] == "FAILED"


# --- when play stops ------------------------------------------------------------------------------


def test_play_stops_when_no_func_applies_for_the_grace_period() -> None:
    fake = _menu()
    nothing = START.replace('return g.scene == "Menu"', 'return g.scene == "Shop"')

    _run(fake, nothing)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED"
    assert verdict["message"].endswith("(executer stopped: no func applied for 0.2s)")
    assert _turns(fake) == []


def test_play_stops_at_the_turn_cap() -> None:
    fake = _menu()
    looking = START.replace(
        'FUNCS = {"start": ("Press Start on the title screen", when_start, run_start)}',
        LOOK + '\nFUNCS = {"look": ("Look at the menu", when_start, run_look)}',
    )

    _run(fake, looking, max_turns_per_step=2)

    assert [turn["picked"] for turn in _turns(fake)] == ["look", "look"]
    (verdict,) = _verdicts(fake)
    assert verdict["message"].endswith("(executer stopped: turn limit)")


def test_play_stops_when_lost_gives_a_reason() -> None:
    fake = _menu()
    losing = START.replace(
        "async def check(g):", 'def lost(g):\n    return "the menu is locked"\n\nasync def check(g):'
    )

    _run(fake, losing)

    assert _turns(fake) == []
    (verdict,) = _verdicts(fake)
    assert verdict["message"].endswith("(executer stopped: lost: the menu is locked)")


def test_play_stops_after_funcs_fail_in_a_row() -> None:
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})  # 눌러도 아무 일이 없다
    quick = START.replace('wait_until(scene() == "Map", 1,', 'wait_until(scene() == "Map", 0.1,')

    _run(fake, quick, max_failures_in_a_row=2)

    turns = _turns(fake)
    assert [turn["outcome"] for turn in turns] == ["failed", "failed"]
    assert turns[0]["reason"].startswith("WAIT_TIMEOUT at run_start step 3 of 3: ")
    (verdict,) = _verdicts(fake)
    assert "(executer stopped: 2 funcs failed in a row (last: start: WAIT_TIMEOUT" in verdict["message"]


# --- which func runs ------------------------------------------------------------------------------


# Menu 에서 `start` 와 `look` 이 함께 적용된다.
TWO = START.replace(
    'FUNCS = {"start": ("Press Start on the title screen", when_start, run_start)}',
    LOOK
    + '\nFUNCS = {\n    "start": ("Press Start to open the map", when_start, run_start),\n'
    '    "look": ("Look at the menu without pressing anything", when_start, run_look),\n}',
)


def test_a_single_func_that_applies_runs_without_asking_jev() -> None:
    fake = _menu()
    jev = FakeJev()

    _run(fake, START, jev=JevClient(post=jev))

    assert jev.bodies == []
    (turn,) = _turns(fake)
    assert turn["picked"] == "start" and turn["decision"] is None


def test_when_several_funcs_apply_jev_picks_one_as_a_macro_decide_would() -> None:
    fake = _menu()
    jev = FakeJev(choice="start")

    _run(fake, TWO, jev=JevClient(post=jev))

    (body,) = jev.bodies
    question = body["questions"]["answer"]
    assert question["type"] == "choice"
    assert question["instructions"] == (
        "Which of these should the player do now to progress this step? The step: Step 1."
    )
    assert question["criteria"] == {
        "start": {"what": "Press Start to open the map"},
        "look": {"what": "Look at the menu without pressing anything"},
        "none_fits": {"what": "None of the other options fits the current state."},
    }
    # keyword 없는 `decide` 처럼 scene 과 화면의 object 전부가 state 로 간다.
    assert body["state"]["scene"] == "Menu"
    assert [obj["path"] for obj in body["state"]["objects"]] == ["Root/Start"]
    (turn,) = _turns(fake)
    assert turn["applicable"] == ["start", "look"] and turn["picked"] == "start"
    assert turn["decision"]["answer"] == "start"
    assert _verdicts(fake)[0]["status"] == "COMPLETED"


def test_jevs_answer_is_followed_even_when_it_is_not_the_first_written() -> None:
    fake = _menu()

    _run(fake, TWO, jev=JevClient(post=FakeJev(choice="look")), max_turns_per_step=1)

    (turn,) = _turns(fake)
    assert turn["picked"] == "look" and fake.actions == []


def test_none_fits_counts_as_no_func_applying_and_play_stops_after_the_grace() -> None:
    fake = _menu()
    jev = FakeJev(choice="none_fits")

    _run(fake, TWO, jev=JevClient(post=jev))

    assert fake.actions == []
    turns = _turns(fake)
    assert turns and all(t["picked"] is None and t["outcome"] == "none_fits" for t in turns)
    (verdict,) = _verdicts(fake)
    assert verdict["message"].endswith("(executer stopped: no func applied for 0.2s)")


def test_when_jev_cannot_answer_the_first_written_func_runs_and_the_error_is_recorded() -> None:
    fake = _menu()

    async def down(body: dict) -> dict:
        raise RuntimeError("decisions endpoint down")

    _run(fake, TWO, jev=JevClient(post=down))

    turn = _turns(fake)[0]
    assert turn["picked"] == "start"
    assert "decisions endpoint down" in turn["decision"]["error"]
    assert _verdicts(fake)[0]["status"] == "COMPLETED"


def test_a_checkpoint_ends_the_turn_without_a_failure() -> None:
    fake = _menu()
    pausing = START.replace("    click(start)\n", '    checkpoint("look first")\n    click(start)\n')

    _run(fake, pausing, max_turns_per_step=1)

    assert fake.actions == []
    (turn,) = _turns(fake)
    assert turn["outcome"] == "paused" and turn["reason"] == "stopped at a checkpoint: look first"


# --- `ask`, `decide` and registered macros ---------------------------------------------------------


def test_ask_goes_to_jev_through_the_tool_loops_client() -> None:
    fake = _menu()
    jev = FakeJev(noul=0.8)
    asking = START.replace(
        "    click(start)\n", '    if ask("Is the start button ready?", button=start):\n        click(start)\n'
    )

    _run(fake, asking, jev=JevClient(post=jev))

    (body,) = jev.bodies
    question = body["questions"]["answer"]
    assert question["type"] == "noul" and question["instructions"] == "Is the start button ready?"
    assert body["state"]["button"]["path"] == "Root/Start"
    assert _verdicts(fake)[0]["status"] == "COMPLETED"


LEFT = 'def left_door() -> None:\n    """Take the left door."""\n    door: object = find(name="Left")\n    click(door)\n'
RIGHT = 'def right_door() -> None:\n    """Take the right door."""\n    door: object = find(name="Right")\n    click(door)\n'

DOORS = '''
def when_doors(g):
    return g.scene == "Hall"

run_doors = """
from macros import left_door, right_door

def run_doors() -> None:
    match decide("Which door leads on?"):
        case "left_door":
            left_door()
        case "right_door":
            right_door()
"""

FUNCS = {"doors": ("Take a door", when_doors, run_doors)}

def done(g):
    return g.scene != "Hall"

async def check(g):
    await g.expect(lambda: g.scene == "Right", "the right door was taken", timeout=0.1)
'''


def _hall() -> FakeGame:
    fake = FakeGame("Hall", {"Root/Left": {"text": "Left"}, "Root/Right": {"text": "Right"}})
    fake.macros = {"left_door": LEFT, "right_door": RIGHT}
    fake.reactions[("press", "Root/Left")] = lambda game: setattr(game, "scene", "Left")
    fake.reactions[("press", "Root/Right")] = lambda game: setattr(game, "scene", "Right")
    return fake


def test_decide_picks_an_imported_registered_macro_through_jev() -> None:
    fake = _hall()
    jev = FakeJev(choice="right_door")

    _run(fake, DOORS, jev=JevClient(post=jev))

    assert _verdicts(fake)[0]["status"] == "COMPLETED"
    (body,) = jev.bodies
    assert body["questions"]["answer"]["criteria"]["right_door"] == {"what": "Take the right door."}


def test_a_module_importing_a_macro_the_build_lacks_fails_without_playing() -> None:
    fake = _hall()
    fake.macros = {"left_door": LEFT}

    _run(fake, DOORS)

    (verdict,) = _verdicts(fake)
    assert verdict["status"] == "FAILED" and verdict["message"].startswith("MODULE-ERROR:")
    assert "`right_door` is imported but cannot be found" in verdict["message"]
    assert fake.actions == []
