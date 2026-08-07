import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.agents.qa.arch import DEFAULT_ARCH, QaArchSpec
from app.agents.qa.reset import DEFAULT_RESET_POLICY, ResetPolicy
from app.agents.qa.runner import QaRunner
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage
from app.llm.models import DEFAULT_MODEL, LLMModel, ReasoningConfig
from app.llm.usage import set_usage_scope
from app.qa.channel import QaRunChannel
from app.qa.envelope import (
    ErrorPayload,
    MessageType,
    RunResult,
    StatusPayload,
    StepStatus,
)
from app.qa.run_config import RunConfig, resolve_run_config
from app.qa.schemas import QaRunScenario, QaSessionRecord
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
        runner_factory: Callable[..., QaRunner] | None = None,
        reset_policy: ResetPolicy | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory or (lambda *, config: QaRunner(config))
        # 시나리오 사이 게임 초기화 정책(seam). 기본은 전체 초기화; 나중에 최소-초기화로 교체.
        self._reset_policy = reset_policy or DEFAULT_RESET_POLICY
        self._channels: dict[str, QaRunChannel] = {}

    async def open(
        self,
        qa_run_id: int,
        game_instance_id: int,
        scenarios: list[QaRunScenario],
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
        prompt_version: str | None = None,
        reasoning: ReasoningConfig | None = None,
        arch: QaArchSpec = DEFAULT_ARCH,
    ) -> tuple[str, RunConfig]:
        """Open a run-scoped session and return its id alongside what it will run with.

        A session is one QA_Run: the run's scenarios execute in order over one
        socket, resetting the game between them. The config is resolved here once
        (a prompt file that cannot be read fails the open, where the caller still
        gets an error, instead of failing a run that has already been reported as
        started) and returned because Orchestration records the resolved form, not
        the request.
        """
        run_config = resolve_run_config(
            model=model,
            language=language,
            prompt_version=prompt_version,
            reasoning=reasoning,
            arch=arch,
        )
        session_id = uuid.uuid4().hex
        record = QaSessionRecord(
            qa_run_id=qa_run_id,
            game_instance_id=game_instance_id,
            scenarios=scenarios,
            run_config=run_config,
        )
        await self._store.save(session_id, record)
        return session_id, run_config

    async def ensure(self, session_id: str) -> int:
        """Return a qa_try_id to stamp connection-level frames with, or raise SessionExpired.

        The first scenario's try. Connection-level ERRORs happen before any
        scenario runs, and Orchestration rejects a frame whose qaTryId has no
        active try, so it must name a real one. Per-scenario frames use their own
        try (see [run]).
        """
        record = await self._load(session_id)
        return record.scenarios[0].qa_try_id

    async def close(self, session_id: str) -> None:
        self._channels.pop(session_id, None)
        await self._store.delete(session_id)

    # --- the run --------------------------------------------------------------

    async def run(self, session_id: str, send: Send) -> None:
        """Drive the run's scenarios in order, resetting the game between them.

        One session is one QA_Run. Each scenario runs on its own channel (stamped
        with that scenario's qa_try_id) to its own deadline; a clean scenario
        closes itself with a terminal STATUS via `finish_run`, a cut-short one is
        closed here. A **failed scenario does not stop the run** — the next one
        still runs (ARTEL-242). Only an operator CANCEL stops the whole run.
        Between scenarios the ResetPolicy puts the game back so the next one starts
        from a known state.
        """
        record = await self._load(session_id)
        total = len(record.scenarios)

        for index, item in enumerate(record.scenarios):
            # Every model call inherits this label; the contextvar rides the task.
            set_usage_scope("QA_RUN", item.qa_try_id)
            channel = QaRunChannel(qa_try_id=item.qa_try_id, send=send)
            self._channels[session_id] = channel

            # Reset before every scenario but the first — the first act of this
            # scenario's try, so the reset frame is attributed to a try about to be
            # active. (Resetting before the first is a future setting.)
            if index > 0:
                await self._reset_policy.between_scenarios(channel, index, total)
                if channel.cancelled:
                    self._channels.pop(session_id, None)
                    await self._send_terminal(
                        channel, StepStatus.CANCELLED, None, "QA run cancelled."
                    )
                    return

            runner = self._runner_factory(config=record.run_config)
            try:
                state, failure = await runner.run_with_deadline(channel, item.scenario)
            finally:
                self._channels.pop(session_id, None)

            if channel.cancelled:
                await self._send_terminal(
                    channel, StepStatus.CANCELLED, None, "QA run cancelled."
                )
                return
            if failure is not None:
                # The agent never closed this scenario, so nothing else will say it
                # ended. Report it and carry on to the next scenario.
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
        # 정상 종료(finish_run)와 같은 2단 요약 형태(steps+cases)를 낸다 — 다운스트림이 종료
        # 사유마다 다른 스키마를 보지 않도록. build_summary가 유일한 생성 지점이다.
        summary = state.build_summary() if state is not None else None
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
            elif message_type == MessageType.KNOWLEDGE_SEARCH_RESULT:
                channel.on_knowledge_search_result(raw)
            elif message_type == MessageType.KNOWLEDGE_EXPAND_RESULT:
                channel.on_knowledge_expand_result(raw)
            elif message_type == MessageType.ERROR:
                # Always accepted, answered or not. ERROR is a legitimate frame in
                # both directions, so answering it with "unsupported inbound frame"
                # would be this side reporting a protocol fault that is not one.
                # An uncorrelated one is logged and dropped: nothing is waiting for
                # it, and the run has no verdict to draw from it either.
                if not channel.on_error(raw):
                    logger.warning(
                        "[QA] inbound ERROR answered no pending request: %r",
                        raw.get("payload"),
                    )
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
