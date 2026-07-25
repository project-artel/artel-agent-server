import asyncio
import contextlib

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.envelope import ErrorPayload, MessageType, outbound_envelope
from app.qa.service import QaExecutionService
from app.sessions.store import SessionExpired


router = APIRouter(tags=["qa"])


class QaContext(BaseModel):
    qa_try_id: int
    game_instance_id: int
    test_scenario_id: int
    # The approved test scenario the Agent executes step by step.
    scenario: ScenarioDraft


class OpenQaSessionRequest(BaseModel):
    context: QaContext
    model: LLMModel = DEFAULT_MODEL
    language: OutputLanguage = DEFAULT_LANGUAGE


class OpenQaSessionResponse(BaseModel):
    session_id: str


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
    session_id = await _service(request.app).open(
        qa_try_id=payload.context.qa_try_id,
        game_instance_id=payload.context.game_instance_id,
        test_scenario_id=payload.context.test_scenario_id,
        scenario=payload.context.scenario,
        model=payload.model,
        language=payload.language,
    )
    return OpenQaSessionResponse(session_id=session_id)


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
