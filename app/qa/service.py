import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.agents.qa.runner import QaRunner
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.channel import QaRunChannel
from app.qa.envelope import (
    ErrorPayload,
    MessageType,
    RunResult,
    StatusPayload,
    StepStatus,
)
from app.qa.schemas import QaSessionRecord
from app.qa.store import QaSessionStore
from app.sessions.store import SessionExpired

logger = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]


class QaExecutionService:
    """Opens QA sessions and runs one agent loop per connected session.

    The loop, not the caller, drives the run: it decides when to look at the game
    and when to act. Inbound frames are handed to the run's channel rather than
    processed as requests, because they are answers to something the agent asked.
    """

    def __init__(
        self,
        store: QaSessionStore,
        runner_factory: Callable[[LLMModel, OutputLanguage, str | None], QaRunner]
        | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory or (
            lambda model, language, prompt_version: QaRunner(
                model=model, language=language, prompt_version=prompt_version
            )
        )
        self._channels: dict[str, QaRunChannel] = {}

    async def open(
        self,
        qa_try_id: int,
        game_instance_id: int,
        test_scenario_id: int,
        scenario: ScenarioDraft,
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
        prompt_version: str | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex
        record = QaSessionRecord(
            qa_try_id=qa_try_id,
            game_instance_id=game_instance_id,
            test_scenario_id=test_scenario_id,
            scenario=scenario,
            model=model,
            language=language,
            prompt_version=prompt_version,
        )
        await self._store.save(session_id, record)
        return session_id

    async def ensure(self, session_id: str) -> int:
        """Return the session's qa_try_id, raising SessionExpired if it is gone.

        Used on WS connect, and to stamp connection-level ERROR frames with the
        real try id (Orchestration rejects frames whose qaTryId has no active try).
        """
        record = await self._load(session_id)
        return record.qa_try_id

    async def close(self, session_id: str) -> None:
        self._channels.pop(session_id, None)
        await self._store.delete(session_id)

    # --- the run --------------------------------------------------------------

    async def run(self, session_id: str, send: Send) -> None:
        """Drive one scenario to completion, sending frames as it goes.

        Returns when the run is over — cleanly, cancelled, or cut short — having
        already sent a terminal STATUS in every case. The caller closes the socket.
        """
        record = await self._load(session_id)
        channel = QaRunChannel(qa_try_id=record.qa_try_id, send=send)
        self._channels[session_id] = channel

        runner = self._runner_factory(
            record.model, record.language, record.prompt_version
        )
        try:
            state, failure = await runner.run_with_deadline(channel, record.scenario)
        finally:
            self._channels.pop(session_id, None)

        if channel.cancelled:
            await self._send_terminal(channel, StepStatus.CANCELLED, None, "QA run cancelled.")
            return
        if failure is not None:
            # The agent never closed the run, so nothing else will say it ended.
            await channel.emit(
                MessageType.ERROR, ErrorPayload(message=failure, code="run_incomplete")
            )
            await self._send_terminal(
                channel, StepStatus.FAILED, RunResult.FAILED, failure, state
            )

    async def _send_terminal(
        self,
        channel: QaRunChannel,
        status: StepStatus,
        result: RunResult | None,
        message: str,
        state=None,
    ) -> None:
        summary = None
        if state is not None:
            passed = sum(1 for item in state.step_results if item.passed)
            summary = {
                "total": state.total_steps,
                "passed": passed,
                "failed": state.total_steps - passed,
                "steps": [item.model_dump() for item in state.step_results],
            }
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(status=status, result=result, message=message, summary=summary),
        )

    # --- inbound --------------------------------------------------------------

    def deliver(self, session_id: str, raw: dict) -> bool:
        """Hand an inbound frame to the running loop.

        False when the frame is unknown or unreadable; the caller reports that
        back over the socket. It never raises, which is the point: a payload this
        cannot parse must not end the run.

        It used to. A single ACTION_RESULT whose shape had drifted from the
        model raised out of here, through the WebSocket handler, and uvicorn
        closed the socket — which Orchestration reads as the agent dying and
        fails the whole try. Orchestration guards its own inbound the same way
        (see QaAgentInboundRouter).
        """
        channel = self._channels.get(session_id)
        if channel is None:
            return False
        message_type = raw.get("type")
        try:
            if message_type == MessageType.GAME_STATE:
                channel.on_game_state(raw)
            elif message_type == MessageType.ACTION_RESULT:
                channel.on_action_result(raw)
            elif message_type == MessageType.CHAT:
                channel.on_chat(raw)
            elif message_type == MessageType.CANCEL:
                channel.on_cancel()
            else:
                return False
        except ValidationError as error:
            logger.warning(
                "[QA] dropped an unreadable %s frame: %s\n  payload: %r",
                message_type,
                error,
                raw.get("payload"),
            )
            return False
        return True

    async def _load(self, session_id: str) -> QaSessionRecord:
        record = await self._store.load(session_id)
        if record is None:
            raise SessionExpired(session_id)
        return record
