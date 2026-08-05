import asyncio
import contextlib

import openai
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app.agents import (
    DEFAULT_LANGUAGE,
    OutputLanguage,
    ScenarioAgentResult,
    ScenarioDraft,
    ScenarioGenerationError,
    ScenarioPlan,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.sessions.channel import ScenarioChannel
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
    # Run scope (ARTEL-206): set when opened from a run dashboard. Optional so
    # pre-run-scope callers keep working.
    run_id: int | None = None
    project_id: int | None = None
    # The run's current scenarios at open (ARTEL-206 Step 6); lets the agent edit
    # existing scenarios by echoing their `scenario_id`. Empty for a fresh run.
    current_scenarios: list[ScenarioPlan] = Field(default_factory=list)


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
    # The run's current scenarios at this turn (ARTEL-206 Step 6), refreshed by
    # the orchestration server so edits target the right existing scenario_id.
    current_scenarios: list[ScenarioPlan] = Field(default_factory=list)


def _service(app) -> SessionService:
    return app.state.session_service


def _result_event(result: ScenarioAgentResult) -> dict:
    return {
        "type": "result",
        "message": result.message,
        "scenarios": [scenario.model_dump() for scenario in result.scenarios],
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
        run_id=payload.run_id,
        project_id=payload.project_id,
        current_scenarios=payload.current_scenarios,
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
    """Drive authoring turns while relaying the agent's case-search frames.

    A turn is no longer a plain request/response: while it runs, the agent may
    send `test_case_search` frames and needs their `test_case_search_result`
    answers back on the same socket. So each turn runs as its own task and this
    coroutine keeps reading, routing inbound frames — search replies to the
    channel, `close` to teardown, a `turn` arriving mid-turn back as a busy
    error. This mirrors `app/api/qa_sessions.py`'s dispatch, with a persistent
    receive so a frame is never lost to a cancellation when a turn completes.
    """
    service = _service(websocket.app)
    await websocket.accept()

    # Frames go out from the turn task and from here; serialise writes so two
    # never interleave on the one socket.
    write_lock = asyncio.Lock()

    async def send(frame: dict) -> None:
        async with write_lock:
            await websocket.send_json(frame)

    channel = ScenarioChannel(send)

    # The turn in flight, or None when idle. The first turn runs from the stored
    # pending input; a missing session surfaces as SessionExpired when it is
    # awaited below, the same signal the old synchronous first turn raised.
    current: asyncio.Task | None = asyncio.create_task(
        service.start_first_turn(session_id, channel)
    )
    receive: asyncio.Task = asyncio.create_task(websocket.receive_json())

    try:
        while True:
            waiters: set[asyncio.Task] = {receive}
            if current is not None:
                waiters.add(current)
            done, _ = await asyncio.wait(
                waiters, return_when=asyncio.FIRST_COMPLETED
            )

            # A finished turn is drained first, so its result is sent before any
            # frame that arrived in the same wake-up is handled.
            if current is not None and current in done:
                finished, current = current, None
                try:
                    result = finished.result()
                except SessionExpired:
                    await send(
                        _error_event(
                            "session_expired", "Session not found or expired."
                        )
                    )
                    break
                except ScenarioGenerationError as error:
                    await send(_error_event("validation_error", str(error)))
                except openai.APIError as error:
                    await send(_error_event("llm_error", str(error)))
                else:
                    # None only from the first turn when there was no pending
                    # input; there is nothing to report and the session waits.
                    if result is not None:
                        await send(_result_event(result))
                continue

            # Otherwise a frame arrived. `.result()` re-raises a disconnect.
            raw = receive.result()
            receive = asyncio.create_task(websocket.receive_json())

            if not isinstance(raw, dict):
                await send(_error_event("bad_request", f"Unsupported frame: {raw!r}"))
                continue

            kind = raw.get("type")

            if kind == "close":
                # Client-initiated termination. Cancel any turn still running,
                # tear the session down, and close the socket this owns.
                if current is not None:
                    current.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await current
                    current = None
                await service.close(session_id)
                await send({"type": "closed"})
                break

            if kind == "turn":
                if current is not None:
                    # One turn at a time: a second while one is in flight would
                    # race two agent loops over the same channel and waiter.
                    await send(
                        _error_event("busy", "A turn is already in progress.")
                    )
                    continue
                try:
                    turn = TurnMessage.model_validate(raw)
                except ValidationError as error:
                    await send(_error_event("bad_request", str(error)))
                    continue
                current = asyncio.create_task(
                    service.run_turn(
                        session_id,
                        channel,
                        turn.user_input,
                        turn.draft,
                        turn.model,
                        turn.locale,
                        turn.current_scenarios,
                    )
                )
                continue

            # Anything else is an inbound case-search reply for the running turn.
            if not channel.deliver(raw):
                await send(_error_event("bad_request", f"Unsupported frame: {raw!r}"))
    except WebSocketDisconnect:
        pass
    finally:
        receive.cancel()
        if current is not None and not current.done():
            current.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receive
        if current is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await current

    with contextlib.suppress(RuntimeError):
        await websocket.close()
