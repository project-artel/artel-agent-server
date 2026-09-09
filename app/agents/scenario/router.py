# -*- coding: utf-8 -*-
"""입구 라우터 — 워크플로 재편 플랜 Step 1.

사용자 말이 무엇인지(잡담·가드레일·질문·저작)를 **케이스 전량을 싣기 전에** 가른다.
현행 구조의 낭비가 정확히 이 자리에 있었다: "안녕하세요" 한마디에도 케이스 74k +
지도 전체가 재읽기로 나갔다(계측 2026-09-08). 라우터는 수백 토큰짜리 호출이라
캐시 대상조차 아니고(최소 1,024 미만), 갈래별로 읽을 만큼만 읽게 한다.

분류가 틀려도 막다른 길이 아니다 — 모르면 AUTHORING 으로 보내고, 그 갈래(본선
워크플로)는 무엇이든 받아서 처리한다(기본값 갈래).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Awaitable, Callable

from pydantic import BaseModel, Field

from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel, ReasoningConfig
from app.prompts import load_prompt

logger = logging.getLogger(__name__)


class Route(str, Enum):
    GREETING = "greeting"
    OFFTOPIC = "offtopic"
    QUESTION = "question"
    AUTHORING = "authoring"
    # 기존 시나리오 수정 — 전용 수정 단계가 받는다. 신규 저작과 갈라 두는 이유:
    # 묶기 단계는 신규 묶기용이라 수정을 태우면 통째 재작성이 된다.
    MODIFY = "modify"


class RouteVerdict(BaseModel):
    route: Route
    # 인사·가드레일일 때만 쓰는 응답 문구. 질문·저작은 다음 갈래가 답한다.
    reply: str = Field(default="")


# 라우터는 판단이 얕아 제일 싼 모델이면 된다. 세션 모델과 무관하게 고정한다 —
# 노드별 모델 혼합의 첫 사례다.
ROUTER_MODEL = LLMModel.claude_haiku_4_5_bedrock

RouterCall = Callable[[str], Awaitable[RouteVerdict]]


async def _call_model(prompt: str) -> RouteVerdict:
    # 추론 끔(max_tokens=0) — 669토큰짜리 분류에 추론이 지연 13초·출력 수백 토큰을
    # 쓰던 실측(2026-09-09). 라우터가 느리면 모든 갈래가 그만큼 늦게 시작한다.
    chat = build_chat_model(ROUTER_MODEL, ReasoningConfig(max_tokens=0))
    chain = chat.with_structured_output(RouteVerdict, method="json_schema")
    return await chain.ainvoke(prompt)


class ScenarioRouter:
    """`caller` 는 검사가 갈아 끼운다 — 라우팅 규칙과 모델 호출을 가른다."""

    def __init__(self, caller: RouterCall | None = None) -> None:
        self._caller = caller or _call_model

    async def route(self, user_input: str, context_hint: str = "") -> RouteVerdict:
        # 분류 문구는 버전 파일에 있다 (`app/prompts/scenario_router/`) — 잠금과
        # 버전 기록을 다른 프롬프트와 같은 방식으로 받기 위해서다.
        prompt = load_prompt("scenario_router", "system").body.format(
            user_input=user_input, context_hint=context_hint or "(none)"
        )
        try:
            verdict = await self._caller(prompt)
        except Exception as error:  # noqa: BLE001 — 라우터 실패가 턴을 죽이면 안 된다
            logger.warning("[scenario] router failed — falling back to authoring: %s", error)
            return RouteVerdict(route=Route.AUTHORING)
        logger.info("[scenario] routed to %s", verdict.route.value)
        return verdict
