"""The project's developer macros on the planner–executer engine (ARTEL-1101).

Orchestration is an `httpx.MockTransport`, the game is `FakeGame`, and the planner's model, the
evaluator and Jev are the fakes `test_planner_executer_steps` uses. Nothing reaches the network.
"""

import asyncio
import logging

import httpx
import pytest

from app.agents.planner_executer import executer as executer_module
from app.agents.planner_executer.developer_macros import (
    DeveloperMacro,
    fetch_developer_macros,
    load_developer_macros,
)
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Planner
from app.agents.planner_executer.prompts import RULES_ROLE, prompt_text
from app.agents.planner_executer.runner import PlannerExecuterRunner
from app.agents.qa.arch import QaArchSpec
from app.qa.jev import JevClient
from app.qa.schemas import QaRunScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS, Asked, FakeJev, _config, _judge, _menu, _scenario
from tests.test_qa_scene_context import SilentResetPolicy

ORCHESTRATION = "http://orchestration.test"

TAP_START = '''def tap_start() -> None:
    """Press Start on the title screen.

    The right move while the menu shows Start."""
    start: object = find(name="Start")
    click(start)
'''

WAIT_FOR_SCENE = '''def wait_for_scene(name: string) -> None:
    """Wait until the scene `name` shows."""
    wait_until(scene() == name, 1, "The scene did not show.")
'''

BROKEN = '''def broken() -> None:
    """Calls something the grammar does not have."""
    explode()
'''

# 이 프로젝트의 build 12 를 도는 런이다. `wait_for_scene` 은 build 11 을 기준으로 쓰였다.
RUN_BUILD = 12


@pytest.fixture(autouse=True)
def _quick(monkeypatch) -> None:
    monkeypatch.setattr(executer_module, "SETTLE_QUIET_SECONDS", 0.001)
    monkeypatch.setattr(executer_module, "IDLE_LOOK_SECONDS", 0.01)


def _item(name: str, source: str, build: str, version: str, label: str | None) -> dict:
    """`DeveloperMacroDetailResponse` as orchestration serializes it."""
    return {
        "name": name,
        "source": source,
        "parameters": [],
        "docstring": "",
        "gameBuildId": build,
        "buildVersion": version,
        "buildLabel": label,
        "version": 1,
        "updatedAt": "2026-10-15T03:00:00Z",
        "updatedBy": {"id": "3", "nickname": "dev"},
    }


def _macro(name: str, source: str, other_build: bool = False) -> DeveloperMacro:
    return DeveloperMacro(name, source, "1.2.0", None, other_build)


def _orchestration(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _fetch(handler, project_id: int | None = 5):
    async def go():
        async with _orchestration(handler) as client:
            return await fetch_developer_macros(project_id, RUN_BUILD, base_url=ORCHESTRATION, client=client)

    return asyncio.run(go())


# --- fetch ---------------------------------------------------------------------------------------


def test_the_projects_developer_macros_are_read_with_their_build() -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "items": [
                    _item("tap_start", TAP_START, "12", "1.2.0", "beta"),
                    _item("wait_for_scene", WAIT_FOR_SCENE, "11", "1.1.0", None),
                ]
            },
        )

    macros = _fetch(handler)

    assert asked == ["/internal/projects/5/developer-macros"]
    assert macros == (
        DeveloperMacro("tap_start", TAP_START, "1.2.0", "beta", other_build=False),
        DeveloperMacro("wait_for_scene", WAIT_FOR_SCENE, "1.1.0", None, other_build=True),
    )


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(404, json={"message": "프로젝트를 찾을 수 없습니다."}),
        lambda request: httpx.Response(200, json={"items": [{"name": "tap_start"}]}),
        _refuse,
    ],
    ids=["server error", "no such project", "unreadable body", "unreachable"],
)
def test_orchestration_failing_leaves_the_run_without_developer_macros(answer, caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert _fetch(answer) == ()

    assert "running without them" in caplog.text


def test_a_run_without_a_project_does_not_ask(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("orchestration was asked")

    with caplog.at_level(logging.WARNING):
        assert _fetch(handler, project_id=None) == ()

    assert "no project_id" in caplog.text


# --- planner and executer ------------------------------------------------------------------------

# Start 를 누르는 developer macro 다. 인자를 받는 판: 무엇을 누를지는 planner 가 묶는다.
PRESS = '''def press(target: object) -> None:
    """Press `target` and wait for the map."""
    click(target)
    wait_until(scene() == "Map", 1, "The press did not open the map.")
'''

# 다른 developer macro 를 가져오는 developer macro.
START_AND_WAIT = '''from macros import tap_start


def start_and_wait() -> None:
    """Press Start, then wait for the map."""
    tap_start()
    wait_until(scene() == "Map", 1, "Start did not open the map.")
'''

# 이 build 에 등록된(agent) macro. developer macro `tap_start` 와 이름만 같고 하는 일이 다르다.
AGENT_TAP_START = '''def tap_start() -> None:
    """Press Quit."""
    quit: object = selector("Root/Quit")
    click(quit)
'''

ONLY_DONE = '''def done() -> bool:
    return scene() == "Map"
'''


class Answered:
    """The planner's model answering with whole texts, so an answer can carry a ```developer block."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.answers.pop(0)


def _answer(source: str, *calls: str) -> str:
    developer = "```developer\n" + "".join(f"{call}\n" for call in calls) + "```\n" if calls else ""
    return f"```python\n{source}```\n{developer}"


def _start_menu() -> FakeGame:
    fake = _menu()
    fake.objects["Root/Quit"] = {"text": "Quit"}
    return fake


def _run(fake: FakeGame, asked, developer_macros: tuple[DeveloperMacro, ...], jev: FakeJev | None = None, **knobs):
    config = _config(no_action_grace_seconds=0.2, **knobs)
    judge, _ = _judge({"passed": True, "reason": "The map opens."})

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(
            config, ask=asked, judge=judge, jev=JevClient(post=jev or FakeJev()), developer_macros=developer_macros
        )
        return await runner.run_with_deadline(fake.channel, _scenario())

    return asyncio.run(go())


def _write(fake: FakeGame, asked, developer_macros: tuple[DeveloperMacro, ...], **knobs):
    config = _config(**knobs)

    async def go():
        fake.push_reading()
        developer = await load_developer_macros(developer_macros)
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel), ask=asked, developer=developer)
        return await planner.write(_scenario(), 1)

    return asyncio.run(go())


def test_the_planner_sees_each_developer_macro_with_its_build() -> None:
    asked = Asked(MACROS)
    macros = (
        DeveloperMacro("tap_start", TAP_START, "1.2.0", "beta", other_build=False),
        DeveloperMacro("wait_for_scene", WAIT_FOR_SCENE, "1.1.0", None, other_build=True),
    )

    _run(_menu(), asked, macros)

    (system, user), = asked.calls
    assert "select the ones this step needs in the ```developer block" in user
    assert "from macros import" not in user
    # 기본 planner prompt 는 v2 이고, 목록과 같은 말을 하는 reuse-first 규칙을 담는다.
    assert _config().prompt_version == "v2"
    assert system == prompt_text(RULES_ROLE, "v2")
    assert "- Reuse first. Before you write an action, read the developer macros the request lists." in system
    assert (
        "- `tap_start()` — written against build 1.2.0 (beta)\n"
        "    Press Start on the title screen.\n"
        "\n"
        "    The right move while the menu shows Start.\n"
    ) in user
    assert (
        "- `wait_for_scene(name: string)` — written against build 1.1.0, a different build from "
        "this run's\n    Wait until the scene `name` shows."
    ) in user


def test_a_parameterless_developer_macro_is_picked_by_jev_and_runs_in_a_step_with_no_def_of_its_own() -> None:
    fake = _menu()
    jev = FakeJev(picks=("developer:tap_start",))

    state, failure = _run(fake, Answered(_answer(ONLY_DONE, "tap_start")), (_macro("tap_start", TAP_START),), jev)

    assert failure is None and state.step_results[0].passed
    (pick,) = jev.asked("choice")[:1]
    options = pick["questions"]["answer"]["criteria"]
    assert set(options) == {"developer:tap_start", "none_fits"}
    assert options["developer:tap_start"]["what"].startswith("Press Start on the title screen.")
    assert fake.frames("MACRO_READ") == []
    (action,) = fake.frames("ACTION")
    assert action["payload"]["message"] == "click(start)"
    assert fake.scene == "Map"


def test_a_developer_macro_with_parameters_runs_with_the_arguments_the_planner_bound() -> None:
    fake = _menu()
    jev = FakeJev(picks=("developer:press",))
    answer = _answer(ONLY_DONE, 'press(find(name="Start"))')

    state, failure = _run(fake, Answered(answer), (_macro("press", PRESS),), jev)

    assert failure is None and state.step_results[0].passed
    description = jev.asked("choice")[0]["questions"]["answer"]["criteria"]["developer:press"]["what"]
    assert description == 'Press `target` and wait for the map.\n\nCalled in this step as `press(find(name="Start"))`.'
    assert fake.actions[0][0] == "move_mouse" and fake.actions[0][1][0] == "Root/Start"
    assert fake.scene == "Map"


@pytest.mark.parametrize(
    ("call", "said"),
    [
        ("press()", "`press` is missing target"),
        ("press(2)", "the developer macro call `press(2)` is refused"),
        ("tap_start", "`tap_start` in the ```developer block is not a developer macro of this project"),
        ("press(", "is not a developer macro's name or a call of one"),
    ],
)
def test_a_refused_developer_call_goes_back_for_a_rewrite(call: str, said: str) -> None:
    asked = Answered(_answer(ONLY_DONE, call), _answer(ONLY_DONE, 'press(selector("Root/Start"))'))

    written = _write(_menu(), asked, (_macro("press", PRESS),))

    assert written.refusal is None and list(written.macros.actions) == ["developer:press"]
    assert len(asked.calls) == 2
    assert said in asked.calls[1][1]


def test_one_developer_macro_bound_twice_gets_two_keys() -> None:
    answer = _answer(ONLY_DONE, 'press(find(name="Start"))', 'press(selector("Root/Start"))')

    written = _write(_menu(), Answered(answer), (_macro("press", PRESS),))

    assert list(written.macros.actions) == ["developer:press", "developer:press#2"]


def test_a_developer_macro_and_an_agent_macro_of_the_same_name_stay_apart() -> None:
    """`from macros import` in the planner's source reaches the build's macro, the ```developer
    block the project's; neither hides the other."""
    fake = _start_menu()
    fake.macros = {"tap_start": AGENT_TAP_START}
    source = "from macros import tap_start\n\n" + MACROS.replace(
        '    start: object = find(name="Start")\n    click(start)\n', "    tap_start()\n"
    )

    written = _write(fake, Answered(_answer(source, "tap_start")), (_macro("tap_start", TAP_START),))

    assert written.refusal is None
    actions = written.macros.actions
    (agent,) = actions["press_start"].imports
    (developer,) = actions["developer:tap_start"].imports
    assert agent.source == AGENT_TAP_START and developer.source == TAP_START
    assert [frame["payload"]["name"] for frame in fake.frames("MACRO_READ")] == ["tap_start"]


def test_the_planner_cannot_import_a_developer_macro() -> None:
    fake = _menu()
    source = "from macros import press\n\n" + MACROS

    written = _write(fake, Answered(_answer(source), _answer(source)), (_macro("press", PRESS),), max_rewrites=1)

    assert written.macros is None
    assert "`press` is imported but cannot be found" in written.refusal


def test_a_developer_macro_importing_another_resolves_among_the_projects_developer_macros() -> None:
    fake = _menu()
    # 같은 이름의 agent macro 가 있어도 developer macro 의 import 는 그쪽으로 가지 않는다.
    fake.macros = {"tap_start": AGENT_TAP_START}
    jev = FakeJev(picks=("developer:start_and_wait",))
    macros = (_macro("start_and_wait", START_AND_WAIT), _macro("tap_start", TAP_START))

    state, failure = _run(fake, Answered(_answer(ONLY_DONE, "start_and_wait")), macros, jev)

    assert failure is None and state.step_results[0].passed
    assert fake.frames("MACRO_READ") == []
    assert fake.scene == "Map"


def test_a_developer_macro_that_does_not_parse_is_skipped(caplog) -> None:
    macros = (_macro("broken", BROKEN), _macro("tap_start", TAP_START))

    with caplog.at_level(logging.WARNING):
        loaded = asyncio.run(load_developer_macros(macros))

    assert list(loaded.usable) == ["tap_start"]
    assert "developer macro broken skipped" in caplog.text


def test_a_run_whose_developer_macros_all_fail_to_parse_shows_none() -> None:
    asked = Asked(MACROS)

    state, failure = _run(_menu(), asked, (_macro("broken", BROKEN),))

    assert failure is None and state.finished
    assert "Developer macros" not in asked.calls[0][1]


# --- the service ---------------------------------------------------------------------------------


class _RecordingRunner:
    def __init__(self, seen: list, options: dict) -> None:
        self._seen = seen
        self._options = options

    async def run_with_deadline(self, channel, scenario):
        self._seen.append(self._options)
        return None, None


def _serve(monkeypatch, arch: QaArchSpec, fetch) -> tuple[list[dict], list]:
    """Two scenarios through `QaExecutionService`; what each runner was built with, and the asks."""
    seen: list[dict] = []
    fetched: list = []

    async def recording_fetch(project_id, game_build_id):
        fetched.append((project_id, game_build_id))
        return await fetch(project_id, game_build_id)

    async def go() -> None:
        service = QaExecutionService(
            store=InMemoryQaSessionStore(),
            runner_factory=lambda *, config, **options: _RecordingRunner(seen, options),
            reset_policy=SilentResetPolicy(),
        )
        scenarios = [
            QaRunScenario(qa_try_id=101, test_scenario_id=1, scenario=_scenario()),
            QaRunScenario(qa_try_id=102, test_scenario_id=2, scenario=_scenario()),
        ]
        session_id, _ = await service.open(
            qa_run_id=9, game_instance_id=1, scenarios=scenarios, arch=arch, project_id=5, game_build_id=RUN_BUILD
        )

        async def send(_frame: dict) -> None:
            return None

        await service.run(session_id, send)

    monkeypatch.setattr("app.qa.service.fetch_developer_macros", recording_fetch)
    asyncio.run(go())
    return seen, fetched


def test_the_service_reads_developer_macros_once_per_run_for_the_planner_executer(monkeypatch) -> None:
    macros = (_macro("tap_start", TAP_START),)

    async def fetch(project_id, game_build_id):
        return macros

    seen, fetched = _serve(monkeypatch, QaArchSpec(engine="planner_executer"), fetch)

    assert fetched == [(5, RUN_BUILD)]
    assert seen == [{"developer_macros": macros}, {"developer_macros": macros}]


def test_the_tool_loop_does_not_read_developer_macros(monkeypatch) -> None:
    async def fetch(project_id, game_build_id):
        raise AssertionError("the tool loop asked for developer macros")

    seen, fetched = _serve(monkeypatch, QaArchSpec(), fetch)

    assert fetched == [] and seen == [{}, {}]


def test_the_run_goes_on_when_orchestration_fails(monkeypatch) -> None:
    async def fetch(project_id, game_build_id):
        async with _orchestration(lambda request: httpx.Response(503)) as client:
            return await fetch_developer_macros(project_id, game_build_id, base_url=ORCHESTRATION, client=client)

    seen, _ = _serve(monkeypatch, QaArchSpec(engine="planner_executer"), fetch)

    assert seen == [{"developer_macros": ()}, {"developer_macros": ()}]

