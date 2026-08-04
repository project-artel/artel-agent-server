import openai
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app.agents import (
    DEFAULT_LANGUAGE,
    OutputLanguage,
    ScenarioAgentResult,
    ScenarioDraft,
    ScenarioGenerationError,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.sessions.service import SessionService
from app.sessions.store import SessionExpired


router = APIRouter(tags=["scenario"])


class OpenSessionRequest(BaseModel):
    unity_context: dict = Field(default_factory=dict)
    game_context: dict = Field(default_factory=dict)
    user_input: str
    model: LLMModel = DEFAULT_MODEL
    # Applies to the whole session, including the first turn (run from the stored
    # pending input when the WS connects), so it must be set here, not only on the
    # per-turn message below.
    locale: OutputLanguage = DEFAULT_LANGUAGE
    # What this session's LLM spend is booked against. Optional so an
    # Orchestration that does not send it yet keeps working — until it does, the
    # usage records carry a null reference.
    test_scenario_id: int | None = None


class OpenSessionResponse(BaseModel):
    session_id: str


class AckResponse(BaseModel):
    ok: bool = True


class TurnMessage(BaseModel):
    type: str = "turn"
    user_input: str
    draft: ScenarioDraft | None = None
    model: LLMModel | None = None
    # Optional mid-session locale switch; None keeps the session's locale.
    locale: OutputLanguage | None = None


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
        locale=payload.locale,
        test_scenario_id=payload.test_scenario_id,
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

            # Client-initiated termination. Approve/decline semantics live in the
            # orchestration layer; the agent server only needs to tear the session
            # down and close the socket it owns.
            if isinstance(raw, dict) and raw.get("type") == "close":
                await service.close(session_id)
                await websocket.send_json({"type": "closed"})
                await websocket.close()
                return

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
                    turn.locale,
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
            except openai.APIError as error:
                await websocket.send_json(_error_event("llm_error", str(error)))
                continue

            await websocket.send_json(_result_event(result))
    except WebSocketDisconnect:
        return
