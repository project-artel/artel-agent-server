"""Jenkins 배포 슬롯의 세션 식별과 drain 상태 확인."""

from uuid import uuid4

from fastapi import APIRouter, Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings


router = APIRouter()


def new_session_id() -> str:
    slot = get_settings().deployment_slot
    identifier = uuid4().hex
    return f"{slot}-{identifier}" if slot else identifier


class InflightRequests:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or scope["path"] in {
            "/health", "/internal/deployment/status"
        }:
            await self.app(scope, receive, send)
            return

        state = scope["app"].state
        state.inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            state.inflight -= 1


@router.get("/deployment/status")
async def deployment_status(request: Request) -> dict:
    state = request.app.state
    slot = get_settings().deployment_slot
    # 생성 요청은 store.save 전에도 집계한다. SCAN 중 시작해 끝난 요청도 놓치지 않도록
    # 신규 요청의 라우팅 전환과 이전 nginx worker 종료 후에만 Jenkins가 이 값을 사용한다.
    idle = bool(slot) and not state.inflight
    if idle:
        for store in state.deployment_stores:
            if await store.has_sessions(slot):
                idle = False
                break
    return {"slot": slot, "idle": idle and state.inflight == 0}
