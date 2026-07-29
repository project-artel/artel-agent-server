"""One unreadable frame must not end the run.

It used to. An ACTION_RESULT whose shape had drifted from the model raised out of
`deliver`, through the WebSocket handler, and uvicorn closed the socket — which
Orchestration reads as the agent dying and fails the whole try. The 2026-07-26
run died exactly this way, on `{"id": 1, "success": true, "error": ""}`.
"""

import asyncio

from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def make_scenario() -> ScenarioDraft:
    return ScenarioDraft(
        title="튜토리얼",
        description="튜토리얼 진입을 확인한다",
        steps=[
            ScenarioStep(
                step=1,
                title="시작",
                state="타이틀 화면",
                action="시작 버튼을 누른다",
                expected="튜토리얼 화면으로 넘어간다",
            )
        ],
    )


class StallingRunner:
    """Holds the run open so inbound frames have a live channel to land on.

    `entered` fires once the service has registered the channel, which is what
    the tests wait on — deterministically, rather than sleeping for it.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.entered = asyncio.Event()
        self.released = asyncio.Event()

    async def run_with_deadline(self, channel, scenario):
        self.entered.set()
        await self.released.wait()


async def _running_service() -> tuple[QaExecutionService, str, asyncio.Task]:
    runner = StallingRunner()
    service = QaExecutionService(
        store=InMemoryQaSessionStore(),
        runner_factory=lambda model, language, prompt_version: runner,
    )
    session_id = await service.open(
        qa_try_id=7,
        game_instance_id=1,
        test_scenario_id=1,
        scenario=make_scenario(),
    )

    async def send(_frame: dict) -> None:
        return None

    task = asyncio.create_task(service.run(session_id, send))
    await runner.entered.wait()
    return service, session_id, task


def test_a_malformed_action_result_does_not_raise() -> None:
    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            # `id` is required on every result item; a frame without it cannot parse.
            handled = service.deliver(
                session_id,
                {
                    "type": "ACTION_RESULT",
                    "payload": {"results": [{"success": True}]},
                },
            )
            assert handled is False
        finally:
            task.cancel()

    asyncio.run(run())


def test_a_malformed_game_state_does_not_raise() -> None:
    """GAME_STATE requires `scene`; a frame without it must be dropped, not fatal."""

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            handled = service.deliver(
                session_id, {"type": "GAME_STATE", "payload": {"interactables": []}}
            )
            assert handled is False
        finally:
            task.cancel()

    asyncio.run(run())


def test_the_run_survives_and_keeps_accepting_frames() -> None:
    """The point of the change: what comes after a bad frame still works."""

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            service.deliver(session_id, {"type": "GAME_STATE", "payload": {}})
            assert task.done() is False

            good = service.deliver(
                session_id,
                {"type": "GAME_STATE", "payload": {"scene": "TitleScene"}},
            )
            assert good is True
        finally:
            task.cancel()

    asyncio.run(run())


def test_a_knowledge_search_result_reaches_the_channel() -> None:
    """The answer to a search has to be routed, or the tool that asked hangs
    until its own timeout and the run pays for it."""

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            handled = service.deliver(
                session_id,
                {
                    "type": "KNOWLEDGE_SEARCH_RESULT",
                    "correlationId": "whatever",
                    "payload": {"query": "q", "model": "e5", "results": []},
                },
            )
            assert handled is True
        finally:
            task.cancel()

    asyncio.run(run())


def test_an_inbound_error_is_accepted_even_when_it_answers_nothing() -> None:
    """ERROR is a legitimate frame in both directions.

    Rejected, it would come back over the socket as "unsupported inbound frame" —
    this side reporting a protocol fault where the other side was reporting a
    problem. Correlated to a pending search it releases that search; otherwise it
    is logged and dropped.
    """

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            handled = service.deliver(
                session_id,
                {"type": "ERROR", "payload": {"message": "knowledge search failed"}},
            )
            assert handled is True
        finally:
            task.cancel()

    asyncio.run(run())


def test_an_unknown_type_is_still_rejected() -> None:
    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            assert service.deliver(session_id, {"type": "NOPE"}) is False
        finally:
            task.cancel()

    asyncio.run(run())
