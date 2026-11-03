"""One unreadable frame must not end the run.

It used to. An ACTION_RESULT whose shape had drifted from the model raised out of
`deliver`, through the WebSocket handler, and uvicorn closed the socket — which
Orchestration reads as the agent dying and fails the whole try. The 2026-07-26
run died exactly this way, on `{"id": 1, "success": true, "error": ""}`.
"""

import asyncio

from app.qa.channel import KNOWLEDGE_SEARCH_TIMEOUT_SECONDS, READING_WAIT_SECONDS
from app.qa.schemas import QaCaseRef, QaRunScenario, QaScenario, QaStep
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def make_scenario() -> QaScenario:
    return QaScenario(
        title="튜토리얼",
        description="튜토리얼 진입을 확인한다",
        steps=[
            QaStep(
                action="시작 버튼을 누른다",
                case_id=1,
                case=QaCaseRef(
                    id=1,
                    precondition="타이틀 화면",
                    test_step="시작",
                    expected="튜토리얼 화면으로 넘어간다",
                ),
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
        runner_factory=lambda **_kwargs: runner,
    )
    session_id, _run_config = await service.open(
        qa_run_id=7,
        game_instance_id=1,
        scenarios=[
            QaRunScenario(qa_try_id=7, test_scenario_id=1, scenario=make_scenario())
        ],
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


def test_an_issue_result_is_accepted_and_waits_for_nothing() -> None:
    """`ISSUE_RESULT` is Orchestration's reply to an `ISSUE` (ARTEL-366). Nothing here waits on
    it; it is accepted so the far side is not told its reply was an unsupported frame."""

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            handled = service.deliver(
                session_id,
                {"type": "ISSUE_RESULT", "correlationId": "m1", "payload": {"type": "ISSUE", "issue_id": "42"}},
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


def test_the_run_builds_its_channel_with_the_production_waits() -> None:
    """런이 세우는 채널은 `READING_WAIT_SECONDS` 와 `KNOWLEDGE_SEARCH_TIMEOUT_SECONDS` 를 쓴다.

    테스트 대부분이 `reading_wait` 와 `search_timeout` 을 짧게 주입한다(ARTEL-1039). 그러면
    운영이 어떤 값으로 도는지는 그 테스트들이 말해 주지 않으므로, `QaExecutionService.run` 이
    실제로 세운 채널을 여기서 본다.

    private 필드를 읽는다. 검색의 상한은 다 기다려 봐야만 밖에서 보이는데 그것이 20초다.
    액션 뒤 대기의 상한은 `tests/test_qa_channel.py` 의
    `test_a_still_screen_is_not_the_game_failing_to_answer` 가 기본값 채널로 직접 잰다.
    """

    async def run() -> None:
        service, session_id, task = await _running_service()
        try:
            channel = service._channels[session_id]
            assert channel._reading_wait == READING_WAIT_SECONDS
            assert channel._search_timeout == KNOWLEDGE_SEARCH_TIMEOUT_SECONDS
        finally:
            task.cancel()

    asyncio.run(run())
