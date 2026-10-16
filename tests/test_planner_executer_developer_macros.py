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
    usable_developer_macros,
)
from app.agents.planner_executer.macros import RegisteredMacros
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

# planner 가 Start 누르기를 developer macro `tap_start` 로 쓴 답이다.
IMPORTING = "from macros import tap_start\n\n" + MACROS.replace(
    '    start: object = find(name="Start")\n    click(start)\n', "    tap_start()\n"
)


def _run(fake: FakeGame, asked: Asked, developer_macros: tuple[DeveloperMacro, ...]):
    config = _config(no_action_grace_seconds=0.2)
    judge, _ = _judge({"passed": True, "reason": "The map opens."})

    async def go():
        fake.push_reading()
        runner = PlannerExecuterRunner(
            config, ask=asked, judge=judge, jev=JevClient(post=FakeJev()), developer_macros=developer_macros
        )
        return await runner.run_with_deadline(fake.channel, _scenario())

    return asyncio.run(go())


def test_the_planner_sees_each_developer_macro_with_its_build() -> None:
    asked = Asked(MACROS)
    macros = (
        DeveloperMacro("tap_start", TAP_START, "1.2.0", "beta", other_build=False),
        DeveloperMacro("wait_for_scene", WAIT_FOR_SCENE, "1.1.0", None, other_build=True),
    )

    _run(_menu(), asked, macros)

    (_system, user), = asked.calls
    assert "`from macros import <name>`" in user
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


def test_an_action_that_imports_a_developer_macro_is_played() -> None:
    fake = _menu()

    state, failure = _run(fake, Asked(IMPORTING), (_macro("tap_start", TAP_START),))

    assert failure is None and state.step_results[0].passed
    # 원문이 런에 실려 왔으므로 content map 에 묻지 않는다.
    assert fake.frames("MACRO_READ") == []
    (action,) = fake.frames("ACTION")
    assert action["payload"]["message"] == "click(start)"
    assert fake.scene == "Map"


def test_a_developer_macro_that_does_not_parse_is_skipped(caplog) -> None:
    fake = _menu()
    macros = (_macro("broken", BROKEN), _macro("tap_start", TAP_START))

    async def go():
        registered = RegisteredMacros(fake.channel, {macro.name: macro.source for macro in macros})
        return await usable_developer_macros(macros, registered)

    with caplog.at_level(logging.WARNING):
        usable = asyncio.run(go())

    assert [item.macro.name for item in usable] == ["tap_start"]
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

