import openai
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app.agents.qa import QaExecutionError
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

    try:
        while True:
            raw = await websocket.receive_json()
            message_type = raw.get("type") if isinstance(raw, dict) else None

            try:
                if message_type == MessageType.GAME_STATE:
                    output = await service.on_game_state(session_id, raw)
                elif message_type == MessageType.ACTION_RESULT:
                    output = await service.on_action_result(session_id, raw)
                elif message_type == MessageType.CANCEL:
                    output = await service.on_cancel(session_id, raw)
                else:
                    await websocket.send_json(
                        _error_frame(
                            "bad_request",
                            f"Unsupported inbound type: {message_type!r}",
                            qa_try_id,
                        )
                    )
                    continue
            except SessionExpired:
                await websocket.send_json(
                    _error_frame(
                        "session_expired", "Session not found or expired.", qa_try_id
                    )
                )
                await websocket.close()
                return
            except ValidationError as error:
                await websocket.send_json(
                    _error_frame("bad_request", str(error), qa_try_id)
                )
                continue
            except QaExecutionError as error:
                await websocket.send_json(
                    _error_frame("agent_error", str(error), qa_try_id)
                )
                continue
            except openai.APIError as error:
                await websocket.send_json(
                    _error_frame("llm_error", str(error), qa_try_id)
                )
                continue
            except Exception as error:  # noqa: BLE001 - keep the socket alive
                # Store/serialization/unexpected LLM errors must not tear the socket
                # down silently; surface an ERROR frame and keep serving.
                await websocket.send_json(
                    _error_frame("internal", str(error), qa_try_id)
                )
                continue

            for frame in output.frames:
                await websocket.send_json(frame)

            if output.terminal:
                await service.close(session_id)
                await websocket.close()
                return
    except WebSocketDisconnect:
        return
