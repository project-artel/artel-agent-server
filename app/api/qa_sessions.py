import asyncio
import contextlib

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, model_validator

from app.agents.qa.arch import DEFAULT_ARCH, QaArchSpec, resolve_arch
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage
from app.llm.models import (
    DEFAULT_MODEL,
    LLMModel,
    ReasoningConfig,
    validate_reasoning,
)
from app.qa.envelope import ErrorPayload, MessageType, outbound_envelope
from app.qa.run_config import RunConfig
from app.qa.schemas import QaRunScenario, QaScenario
from app.qa.service import QaExecutionService
from app.sessions.store import SessionExpired


router = APIRouter(tags=["qa"])


class QaContext(BaseModel):
    """QA_Run 세션이 실행할 것 (ARTEL-258).

    런 단위: `scenarios`는 런의 시나리오들(각자 qa_try_id + 실행 본문)이며 순서대로 실행된다.
    레거시 단일 시나리오 호출(qa_try_id/test_scenario_id/scenario)도 받아 1-시나리오 런으로
    정규화한다 — Orche가 run-scoped로 바뀌기 전까지의 하위호환.
    """

    game_instance_id: int
    qa_run_id: int | None = None
    scenarios: list[QaRunScenario] = []

    # --- 레거시 단일 시나리오 (하위호환) ---
    qa_try_id: int | None = None
    test_scenario_id: int | None = None
    # The approved test scenario the Agent executes: an ordered Step list (재설계).
    scenario: QaScenario | None = None

    @model_validator(mode="after")
    def normalize(self) -> "QaContext":
        if not self.scenarios:
            if (
                self.scenario is None
                or self.qa_try_id is None
                or self.test_scenario_id is None
            ):
                raise ValueError(
                    "QA context requires scenarios[] or a single "
                    "(qa_try_id, test_scenario_id, scenario)"
                )
            self.scenarios = [
                QaRunScenario(
                    qa_try_id=self.qa_try_id,
                    test_scenario_id=self.test_scenario_id,
                    scenario=self.scenario,
                )
            ]
            if self.qa_run_id is None:
                # 단일 시나리오 런의 run id는 그 try id로 대신한다(전용 run이 없으므로).
                self.qa_run_id = self.qa_try_id
        elif self.qa_run_id is None:
            raise ValueError("run-scoped QA context requires qa_run_id")
        return self


class OpenQaSessionRequest(BaseModel):
    context: QaContext
    model: LLMModel = DEFAULT_MODEL
    language: OutputLanguage = DEFAULT_LANGUAGE
    # Pins this run to one prompt version (a directory under
    # app/prompts/qa_run/), so two runs can be compared. Omit it to take
    # QA_PROMPT_VERSION, and failing that the newest version.
    prompt_version: str | None = None
    reasoning: ReasoningConfig | None = None
    # The agent's structure — loop bounds, per-run allowances, vision, middleware.
    # Omit it for today's structure; set it to compare two structures without
    # deploying twice. See `app/agents/qa/arch.py`.
    arch: QaArchSpec = DEFAULT_ARCH

    @model_validator(mode="after")
    def validate_against_model(self) -> "OpenQaSessionRequest":
        """Refuse a request the model cannot honour, rather than downgrading it.

        Both checks are here rather than at the service so the caller gets a 422
        naming the field, and so a run is never opened under settings it will not
        actually use.
        """
        validate_reasoning(self.model, self.reasoning)
        resolve_arch(self.arch, self.model)
        return self


class OpenQaSessionResponse(BaseModel):
    session_id: str
    # What the run will actually use, with every alias and "auto" already settled.
    # Orchestration stores this against the try; it is the only record of what a
    # run was executed with once the request is gone.
    run_config: RunConfig


def _service(app) -> QaExecutionService:
    return app.state.qa_session_service


def _error_frame(code: str, detail: str, qa_try_id: int = 0) -> dict:
    """A minimal ERROR envelope for connection-level failures.

    `qa_try_id` must be the real try once the session is known: Orchestration
    rejects a frame whose qaTryId has no active try, and that rejection kills the
    WebSocket and fails the whole run. 0 is only for the pre-session case, where
    there is no try to name.
    """
    return outbound_envelope(
        MessageType.ERROR,
        qa_try_id=qa_try_id,
        sequence=0,
        payload=ErrorPayload(message=detail, code=code),
    )


@router.post("/qa-sessions", response_model=OpenQaSessionResponse)
async def open_qa_session(
    payload: OpenQaSessionRequest,
    request: Request,
) -> OpenQaSessionResponse:
    session_id, run_config = await _service(request.app).open(
        qa_run_id=payload.context.qa_run_id,
        game_instance_id=payload.context.game_instance_id,
        scenarios=payload.context.scenarios,
        model=payload.model,
        language=payload.language,
        prompt_version=payload.prompt_version,
        reasoning=payload.reasoning,
        arch=payload.arch,
    )
    return OpenQaSessionResponse(session_id=session_id, run_config=run_config)


@router.websocket("/qa-sessions/{session_id}")
async def qa_session_ws(websocket: WebSocket, session_id: str) -> None:
    service = _service(websocket.app)
    await websocket.accept()

    try:
        qa_try_id = await service.ensure(session_id)
    except SessionExpired:
        await websocket.send_json(
            _error_frame("session_expired", "Session not found or expired.")
        )
        await websocket.close()
        return

    # The agent sends from its own task while this one reads, so writes are
    # serialised. Two frames interleaved on one socket is a protocol error.
    write_lock = asyncio.Lock()

    async def send(frame: dict) -> None:
        async with write_lock:
            await websocket.send_json(frame)

    # The run drives itself; this coroutine only feeds it what arrives.
    run = asyncio.create_task(service.run(session_id, send))

    try:
        while not run.done():
            receive = asyncio.create_task(websocket.receive_json())
            done, _ = await asyncio.wait(
                {receive, run}, return_when=asyncio.FIRST_COMPLETED
            )
            if receive not in done:
                receive.cancel()
                break
            raw = receive.result()
            if not isinstance(raw, dict) or not service.deliver(session_id, raw):
                await send(
                    _error_frame(
                        "bad_request", f"Unsupported inbound frame: {raw!r}", qa_try_id
                    )
                )
    except WebSocketDisconnect:
        run.cancel()
        return
    finally:
        if not run.done():
            run.cancel()

    with contextlib.suppress(asyncio.CancelledError):
        await run
    await service.close(session_id)
    await websocket.close()
    return
