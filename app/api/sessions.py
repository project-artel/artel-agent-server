import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app.agents.errors import ScenarioGenerationError
from app.agents.scenario_schemas import ScenarioAgentResult, ScenarioDraft
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.openrouter_client import LLMClientError
from app.sessions.service import SessionService
from app.sessions.store import SessionExpired


router = APIRouter(tags=["scenario"])


class OpenSessionRequest(BaseModel):
    unity_context: dict = Field(default_factory=dict)
    game_context: dict = Field(default_factory=dict)
    user_input: str
    model: LLMModel = DEFAULT_MODEL


class OpenSessionResponse(BaseModel):
    session_id: str


class AckResponse(BaseModel):
    ok: bool = True


class TurnMessage(BaseModel):
    type: str = "turn"
    user_input: str
    draft: ScenarioDraft | None = None
    model: LLMModel | None = None


def _service(app) -> SessionService:
    return app.state.session_service


def _result_event(result: ScenarioAgentResult) -> dict:
    return {
        "type": "result",
        "message": result.message,
        "scenario": result.scenario.model_dump(),
    }


def _error_event(code: str, detail: str) -> dict:
    return {"type": "error", "code": code, "detail": detail}


@router.post("/sessions", response_model=OpenSessionResponse)
async def open_session(
    payload: OpenSessionRequest,
    request: Request,
) -> OpenSessionResponse:
    session_id = await _service(request.app).open(
        unity_context=payload.unity_context,
        game_context=payload.game_context,
        user_input=payload.user_input,
        model=payload.model,
    )
    return OpenSessionResponse(session_id=session_id)


@router.post("/sessions/{session_id}/approve", response_model=AckResponse)
async def approve_session(session_id: str, request: Request) -> AckResponse:
    await _service(request.app).close(session_id)
    return AckResponse()


@router.post("/sessions/{session_id}/decline", response_model=AckResponse)
async def decline_session(session_id: str, request: Request) -> AckResponse:
    await _service(request.app).close(session_id)
    return AckResponse()


@router.websocket("/sessions/{session_id}")
async def session_ws(websocket: WebSocket, session_id: str) -> None:
    service = _service(websocket.app)
    await websocket.accept()

    try:
        first = await service.start_first_turn(session_id)
    except SessionExpired:
        await websocket.send_json(
            _error_event("session_expired", "Session not found or expired.")
        )
        await websocket.close()
        return
    if first is not None:
        await websocket.send_json(_result_event(first))

    try:
        while True:
            raw = await websocket.receive_json()
            try:
                turn = TurnMessage.model_validate(raw)
            except ValidationError as error:
                await websocket.send_json(_error_event("bad_request", str(error)))
                continue

            try:
                result = await service.run_turn(
                    session_id,
                    turn.user_input,
                    turn.draft,
                    turn.model,
                )
            except SessionExpired:
                await websocket.send_json(
                    _error_event("session_expired", "Session not found or expired.")
                )
                await websocket.close()
                return
            except ScenarioGenerationError as error:
                await websocket.send_json(
                    _error_event("validation_error", str(error))
                )
                continue
            except (LLMClientError, httpx.HTTPError) as error:
                await websocket.send_json(_error_event("llm_error", str(error)))
                continue

            await websocket.send_json(_result_event(result))
    except WebSocketDisconnect:
        return
