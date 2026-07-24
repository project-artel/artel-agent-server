import uuid
from dataclasses import dataclass, field

from app.agents.base import AgentContext
from app.agents.qa import (
    QaActRequest,
    QaExecutionAgent,
    QaVerifyRequest,
)
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.envelope import (
    ActionPayload,
    ActionResultPayload,
    CancelPayload,
    ErrorPayload,
    GameStatePayload,
    JsonRpcAction,
    LogCategory,
    LogPayload,
    MessageType,
    RunResult,
    StatusPayload,
    StepStatus,
    outbound_envelope,
)
from app.qa.schemas import QaSessionRecord, QaStepResult
from app.qa.store import QaSessionStore
from app.sessions.store import SessionExpired


@dataclass
class QaTurnOutput:
    """Frames to send for one inbound message, plus whether the run is terminal."""

    frames: list[dict] = field(default_factory=list)
    terminal: bool = False


class QaExecutionService:
    def __init__(
        self,
        store: QaSessionStore,
        agent: QaExecutionAgent | None = None,
    ) -> None:
        self._store = store
        self._agent = agent or QaExecutionAgent()

    async def open(
        self,
        qa_try_id: int,
        game_instance_id: int,
        test_scenario_id: int,
        scenario: ScenarioDraft,
        model: LLMModel = DEFAULT_MODEL,
        language: OutputLanguage = DEFAULT_LANGUAGE,
    ) -> str:
        session_id = uuid.uuid4().hex
        record = QaSessionRecord(
            qa_try_id=qa_try_id,
            game_instance_id=game_instance_id,
            test_scenario_id=test_scenario_id,
            scenario=scenario,
            model=model,
            language=language,
        )
        await self._store.save(session_id, record)
        return session_id

    async def ensure(self, session_id: str) -> None:
        """Raise SessionExpired if the session is gone (used on WS connect)."""
        await self._load(session_id)

    async def close(self, session_id: str) -> None:
        await self._store.delete(session_id)

    # --- inbound handlers -----------------------------------------------------

    async def on_game_state(self, session_id: str, raw: dict) -> QaTurnOutput:
        record = await self._load(session_id)
        payload = GameStatePayload.model_validate(raw.get("payload") or {})
        record.latest_game_state = payload.state

        frames: list[dict] = []
        # A fresh scene while we still owe the current step its action -> act now.
        if record.phase == "need_action":
            frames += await self._act(session_id, record)
        await self._store.save(session_id, record)
        return QaTurnOutput(frames=frames)

    async def on_action_result(self, session_id: str, raw: dict) -> QaTurnOutput:
        record = await self._load(session_id)
        payload = ActionResultPayload.model_validate(raw.get("payload") or {})
        correlation = raw.get("correlationId")

        if record.phase != "need_result":
            frame = self._frame(
                record,
                MessageType.ERROR,
                ErrorPayload(
                    message="No action is awaiting a result.",
                    code="UNEXPECTED_RESULT",
                ),
            )
            await self._store.save(session_id, record)
            return QaTurnOutput(frames=[frame])

        if (
            correlation
            and record.last_action_message_id
            and correlation != record.last_action_message_id
        ):
            frame = self._frame(
                record,
                MessageType.ERROR,
                ErrorPayload(
                    message="Result correlationId does not match the pending action.",
                    code="CORRELATION_MISMATCH",
                ),
                correlation_id=correlation,
            )
            await self._store.save(session_id, record)
            return QaTurnOutput(frames=[frame])

        frames = await self._verify(session_id, record, payload)

        terminal = False
        if record.current_step < len(record.scenario.steps):
            record.phase = "need_action"
            frames += await self._act(session_id, record)
        else:
            frames += self._terminal(record)
            terminal = True

        await self._store.save(session_id, record)
        return QaTurnOutput(frames=frames, terminal=terminal)

    async def on_cancel(self, session_id: str, raw: dict) -> QaTurnOutput:
        record = await self._load(session_id)
        payload = CancelPayload.model_validate(raw.get("payload") or {})
        record.phase = "done"
        frame = self._frame(
            record,
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.CANCELLED,
                message=payload.message or "QA run cancelled.",
            ),
        )
        await self._store.save(session_id, record)
        return QaTurnOutput(frames=[frame], terminal=True)

    # --- step machinery -------------------------------------------------------

    async def _act(self, session_id: str, record: QaSessionRecord) -> list[dict]:
        step = record.scenario.steps[record.current_step]
        result = await self._agent.act(
            QaActRequest(
                scenario_title=record.scenario.title,
                scenario_description=record.scenario.description,
                step=step,
                game_state=record.latest_game_state,
                model=record.model,
                language=record.language,
            ),
            AgentContext(session_id=session_id),
        )

        # Params assembled from what the agent grounded in the scene: the target
        # id first (when present), then any literal arguments. No fixed method
        # vocabulary is assumed here.
        json_actions = [
            JsonRpcAction(
                id=index + 1,
                method=planned.method,
                params=(
                    ([] if planned.target_id is None else [planned.target_id])
                    + list(planned.arguments)
                ),
            )
            for index, planned in enumerate(result.actions)
        ]

        started = self._frame(
            record,
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.STARTED,
                step=step.step,
                message=f"step {step.step} started",
            ),
        )
        thought = self._frame(
            record,
            MessageType.LOG,
            LogPayload(
                category=LogCategory.THOUGHT, message=result.thought, step=step.step
            ),
        )
        action = self._frame(
            record,
            MessageType.ACTION,
            ActionPayload(
                message=result.action_message, step=step.step, actions=json_actions
            ),
        )
        record.last_action_message_id = action["messageId"]
        record.phase = "need_result"
        return [started, thought, action]

    async def _verify(
        self, session_id: str, record: QaSessionRecord, action_result: ActionResultPayload
    ) -> list[dict]:
        step = record.scenario.steps[record.current_step]
        result = await self._agent.evaluate(
            QaVerifyRequest(
                step=step,
                game_state=record.latest_game_state,
                action_result=action_result,
                model=record.model,
                language=record.language,
            ),
            AgentContext(session_id=session_id),
        )

        validation = self._frame(
            record,
            MessageType.LOG,
            LogPayload(
                category=LogCategory.VALIDATION, message=result.reasoning, step=step.step
            ),
        )
        verdict = self._frame(
            record,
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED if result.passed else StepStatus.FAILED,
                step=step.step,
                message=result.verdict_message,
            ),
        )
        record.step_results.append(
            QaStepResult(step=step.step, passed=result.passed, message=result.verdict_message)
        )
        record.current_step += 1
        return [validation, verdict]

    def _terminal(self, record: QaSessionRecord) -> list[dict]:
        total = len(record.step_results)
        passed = sum(1 for r in record.step_results if r.passed)
        all_passed = passed == total
        record.phase = "done"
        frame = self._frame(
            record,
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED,
                result=RunResult.PASSED if all_passed else RunResult.FAILED,
                message=f"QA completed: {passed} of {total} steps passed.",
                summary={
                    "total": total,
                    "passed": passed,
                    "failed": total - passed,
                    "steps": [r.model_dump() for r in record.step_results],
                },
            ),
        )
        return [frame]

    # --- helpers --------------------------------------------------------------

    def _frame(
        self,
        record: QaSessionRecord,
        message_type: MessageType,
        payload,
        correlation_id: str | None = None,
    ) -> dict:
        record.sequence += 1
        return outbound_envelope(
            message_type,
            record.qa_try_id,
            record.sequence,
            payload,
            correlation_id=correlation_id,
        )

    async def _load(self, session_id: str) -> QaSessionRecord:
        record = await self._store.load(session_id)
        if record is None:
            raise SessionExpired(session_id)
        return record
