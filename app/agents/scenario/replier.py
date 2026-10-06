# -*- coding: utf-8 -*-
"""인사·가드레일 문구 노드.

지금까지는 **라우터가 갈래와 문구를 같이** 냈다(`RouteVerdict.reply`). 그 구조는 라우터가
자유 텍스트를 낼 수 있을 때만 선다. 결정 전용 모델(Jev)은 선택지 중 하나를 고를 뿐 문장을
못 쓰므로, 갈래를 가르는 일과 문구를 쓰는 일을 갈라 둔다.

**이 노드는 전체 트래픽의 1~2%에서만 돈다** — 실측 163줄에서 `greeting` 2건, `offtopic` 0건.
그래서 여기의 지연은 흔한 길의 지연이 아니고, 모델을 아끼지 않아도 된다.

문구를 못 받았을 때 침묵하지 않는다. `agent.py` 가 이미 쥐고 있던 고정 문구가 마지막
그물이다 — 문구 노드가 죽어도 사용자는 답을 받는다.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from app.agents.scenario.router import Route
from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel, ReasoningConfig
from app.prompts import load_prompt

logger = logging.getLogger(__name__)

# 라우터와 같은 모델로 고정한다. 한두 문장을 쓰는 일이고, 세션 모델(비쌀 수 있다)을 끌어올
# 이유가 없다 — 노드별 모델 혼합의 둘째 사례다.
REPLY_MODEL = LLMModel.claude_haiku_4_5_bedrock

# 문구 노드가 답을 못 줄 때의 마지막 그물. 라우터가 문구를 쓰던 시절의 고정 문구와 같다.
FALLBACK = {
    Route.GREETING: (
        "안녕하세요! 게임에서 확인하고 싶은 흐름을 말씀해 주시면 순서대로 정리한 시나리오로 만들어 드려요."
    ),
    Route.OFFTOPIC: "그건 제가 도와드리기 어려워요. 이 게임에서 테스트할 흐름을 말씀해 주시면 바로 도와드릴게요.",
}

ReplyCall = Callable[[str], Awaitable[str]]


def _as_text(content: object) -> str:
    """Bedrock 은 본문을 **블록 리스트**로 돌려준다 — 문자열만 받으면 조용히 빈 문구가 된다.

    실측(2026-10-06): 문자열만 처리했을 때 여섯 건이 전부 폴백으로 떨어졌고, 로그만 보면
    "모델이 빈 답을 줬다" 로 보였다. 모델은 답했고 받는 쪽이 못 읽었다.
    """
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(block.get("text", ""))
        return "".join(parts).strip()
    return ""


async def _call_model(prompt: str) -> str:
    # 추론은 안 켠다. 한두 문장을 쓰는 데 추론 예산을 쓰면 이 노드만 라우터보다 느려진다.
    chat = build_chat_model(REPLY_MODEL, ReasoningConfig(max_tokens=0))
    answer = await chat.ainvoke(prompt)
    return _as_text(answer.content)


class ScenarioReplier:
    """`caller` 는 검사가 갈아 끼운다 — 문구 규칙과 모델 호출을 가른다."""

    def __init__(self, caller: ReplyCall | None = None) -> None:
        self._caller = caller or _call_model

    async def write(self, route: Route, user_input: str) -> str:
        if route not in FALLBACK:
            raise ValueError(f"문구를 쓸 갈래가 아니다: {route}")
        prompt = load_prompt("scenario_reply", "system").body.format(
            route=route.value, user_input=user_input
        )
        try:
            reply = await self._caller(prompt)
        except Exception as error:  # noqa: BLE001 — 문구 실패가 턴을 죽이면 안 된다
            logger.warning("[scenario] reply node failed — using fallback: %s", error)
            return FALLBACK[route]
        if not reply:
            logger.warning("[scenario] reply node returned nothing — using fallback")
            return FALLBACK[route]
        return reply
