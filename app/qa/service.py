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
from app.qa.scene_context import fetch_scene_context
from app.qa.schemas import QaRunScenario, QaSessionRecord
from app.qa.screen_verdict import ScreenSelectorAdjudicator
from app.qa.store import QaSessionStore
from app.sessions.store import SessionExpired

logger = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]


def _build_adjudicator(run_config: RunConfig) -> ScreenSelectorAdjudicator:
    """이 런의 모델로 판정기를 세운다 (ARTEL-656).

    런의 모델을 쓰는 것은 판정이 요약이 아니라 판단이기 때문이다 — 게다가 캡처를 봐야
    하는 판단이라, 그 게임을 상대할 만하다고 이미 고른 모델이 여기서도 맞다. 값싼 모델로
    못박은 `qa_compaction` 과 다른 판단이고, 차이는 일의 성격에 있다.

    프롬프트 버전은 `SCREEN_VERDICT_PROMPT_VERSION` 이 정한다. 런의 `prompt_version` 을
    끌어 쓰지 않는다 — 그것은 QA agent 의 프롬프트 번호이고, 두 프롬프트는 서로 다른
    파일에 서로 다른 속도로 산다.
    """
    return ScreenSelectorAdjudicator(model=run_config.model)


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
        adjudicator_factory: Callable[[RunConfig], ScreenSelectorAdjudicator] | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory or (lambda *, config: QaRunner(config))
        # 시나리오 사이 게임 초기화 정책(seam). 기본은 전체 초기화; 나중에 최소-초기화로 교체.
        self._reset_policy = reset_policy or DEFAULT_RESET_POLICY
        # 화면 제안을 판정하는 자리(seam, ARTEL-656). 채널과 나란히 두고 함께 버린다 —
        # 판정은 QA 런의 일이 아니므로 `QaRunChannel` 안에 살지 않는다.
        self._adjudicator_factory = adjudicator_factory or _build_adjudicator
        self._channels: dict[str, QaRunChannel] = {}
        self._adjudicators: dict[str, ScreenSelectorAdjudicator] = {}

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
        project_id: int | None = None,
        game_build_id: int | None = None,
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
            project_id=project_id,
            game_build_id=game_build_id,
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
        adjudicator = self._adjudicators.pop(session_id, None)
        if adjudicator is not None:
            # 소켓이 사라진 뒤에도 모델 호출이 계속 도는 것을 막는다. 끊긴 판정은 답 없이
            # 끝나고, 그것은 지도를 종전대로 두는 결과다.
            adjudicator.close()
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
            # 시나리오마다 새로 세운다. 판정기는 상태를 안 들지만 도는 task 를 들고,
            # 그 task 는 자기를 띄운 시나리오의 채널로 답한다 — 시나리오가 바뀌면 그
            # 채널은 이미 남의 것이다.
            previous = self._adjudicators.get(session_id)
            if previous is not None:
                previous.close()
            self._adjudicators[session_id] = self._adjudicator_factory(record.run_config)

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

            # 한 번만, 시나리오가 시작하기 전에 (ARTEL-612). 턴마다도 관측마다도 아니다 —
            # 빌드의 씬 지도와 앵커 지식은 런이 도는 동안 바뀌지 않는다.
            #
            # 시나리오마다인 것은 지식 스코프가 `qa_try` 단위이기 때문이다. 세션 하나가
            # 여러 시나리오를 돌고 각 시나리오가 자기 try 를 들고 다니므로, 한 번만 불러
            # 돌려쓰면 두 번째 시나리오가 첫 번째의 스코프로 읽은 지식을 본다.
            #
            # 실패해도 `None` 일 뿐 예외가 나오지 않는다. 어드바이저리 조회 때문에 런이
            # 시작하지 못하는 쪽이, 조언 없이 도는 런보다 나쁘다.
            #
            # `SceneMemory` 에 얹는 것은 화면을 그리는 자리가 거기 하나이기 때문이다.
            # 도구 결과 셋과 압축 원장이 모두 `channel.scene.render` 를 지나가므로, 여기
            # 한 번 얹으면 그 넷이 전부 블록을 갖는다. 런너를 거쳐 내려보내면 같은 값을
            # 네 호출자에게 따로 건네야 한다.
            channel.scene.scene_context = await fetch_scene_context(
                project_id=record.project_id,
                game_build_id=record.game_build_id,
                qa_try_id=item.qa_try_id,
            )

            runner = self._runner_factory(config=record.run_config)
            try:
                state, failure = await runner.run_with_deadline(channel, item.scenario)
            finally:
                self._channels.pop(session_id, None)
                # 이 시나리오의 try 는 끝났다. 아직 도는 판정이 있어도 답을 실을 자리가
                # 없으므로 끊는다 — 답하지 않는 것은 지도를 종전대로 두는 결과다.
                ending = self._adjudicators.pop(session_id, None)
                if ending is not None:
                    ending.close()

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
            elif message_type == MessageType.PULSE:
                channel.on_pulse(raw)
            elif message_type == MessageType.KNOWLEDGE_SEARCH_RESULT:
                channel.on_knowledge_search_result(raw)
            elif message_type == MessageType.KNOWLEDGE_EXPAND_RESULT:
                channel.on_knowledge_expand_result(raw)
            elif message_type == MessageType.KNOWLEDGE_WRITE_RESULT:
                channel.on_knowledge_write_result(raw)
            elif message_type == MessageType.SCREEN_SELECTOR_PROPOSAL:
                # 한 프레임을 둘이 읽는다. 채널은 곁들여 실려 온 화면 판정만 가져가고
                # (ARTEL-657), 질문 자체에 답하는 것은 판정기다(ARTEL-656).
                #
                # 그래도 반드시 받아야 한다. 모르는 타입은 `False` 로 떨어져 "unsupported
                # inbound frame" 으로 답하는데, 이 프레임은 QA agent 에게 화면 판정을 싣고
                # 오는 유일한 통로다 — 거절하면 화면이 영영 안 보인다.
                channel.on_screen_selector_proposal(raw)
                # **아무것도 기다리지 않는다.** 이 loop 가 판정 하나를 기다리면 그동안
                # `PULSE` 도 `ACTION_RESULT` 도 안 들어오고, 그것이 곧 런이 서는 것이다.
                adjudicator = self._adjudicators.get(session_id)
                if adjudicator is not None:
                    adjudicator.answer_later(channel, raw)
            elif message_type == MessageType.SCREEN_SELECTOR_RESULT:
                channel.on_screen_selector_result(raw)
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
