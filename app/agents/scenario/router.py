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
from app.config import get_settings
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
    # 어느 쪽인지 **고르지 않고 되묻는다.** 다섯 갈래 밖의 결과이고 결정 모델 라우터만 낸다.
    #
    # 조용히 AUTHORING 으로 내리는 쪽도 안전하지만(그 갈래는 덮어쓰지 않는다) 사용자는 자기가
    # 고치라고 한 것이 새로 생긴 이유를 모른다. 모르는 자리를 코드가 메우지 않고 묻는다.
    ASK = "ask"


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


# ── 결정 전용 모델 라우터 (ARTEL-944) ─────────────────────────────────────────

JEV_MODEL = "typesafe/jev-1.13"
JEV_DECISIONS_PATH = "/api/alpha/decisions"

# 갈래가 `modify` 인데 대상이 `existing` 이 아니면 되묻는다.
#
# **조건을 한쪽에만 건다.** 반대 방향(갈래 authoring · 대상 existing)에도 걸어 재 봤더니
# 163줄에서 15건을 되묻고 그중 13건은 authoring 이 정답이었다 — 헛경보다. 오분류의 비용이
# 방향마다 다르므로 규칙도 비대칭이어야 한다(실측 2026-10-06).
_ASK_WHEN_TARGET_IS_NOT = "existing"


class JevScenarioRouter(ScenarioRouter):
    """갈래를 **고르는 일**만 하는 모델로 분류한다.

    지금까지 쓰던 범용 대화 모델은 선택지 하나를 고르려고 문장을 쓰는 기계였다. 실측
    (실제 사용자 말 163줄, ARTEL-944):

        지연 중앙값 1,702ms → 240ms · 판정당 $0.0020140 → $0.0001152
        정확도 95.7% → 98.2%, 답을 낸 자리에서는 100%

    **확률에 임계값을 걸지 않는다.** 그 방식을 먼저 시도했다가 버렸다 — 같은 입력을 네 번
    물으면 확률이 최대 0.100 움직여 안전 구간(0.090)보다 잡음이 컸고, `question` 갈래의
    혼동은 임계값으로 아예 고칠 수 없었다. 대신 `target` 을 선택지로 한 번 더 물어 두 답이
    어긋날 때만 되묻는다.

    `reply` 는 비운다. 이 모델은 자유 텍스트를 못 낸다(문서: *Jev does not produce reasoning
    traces, explanations, or free-form text*). 인사·가드레일 문구는 `ScenarioReplier` 가 쓴다.
    """

    def __init__(self, caller: RouterCall | None = None) -> None:
        # 부모의 `caller` 는 렌더된 프롬프트 문자열을 받는다. 이쪽은 선택지 구조를 보내므로
        # 그 자리를 쓰지 않고, 검사가 갈아 끼울 자리는 `_decide` 다.
        super().__init__(caller=caller)
        self._http: object | None = None

    def _client(self):
        """연결을 재사용한다. 호출마다 새로 열면 매번 TLS 악수를 다시 한다 — 실측에서 182줄
        기준 지연 중앙값이 **334ms 대 240ms** 로 벌어졌다(2026-10-06). 입구 라우터의 값어치가
        지연이라, 그 94ms 를 연결 설정에 쓰면 바꾼 이유가 깎인다.

        `SessionService` 가 앱 기동 때 한 번 만들어지므로 이 인스턴스는 앱 수명만큼 산다
        (`app/main.py` 의 `app.state.session_service`). `app/llm/usage.py` 가 같은 모양이다.
        """
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=get_settings().scenario_router_timeout_seconds,
                # **따뜻한 연결 수가 동시 턴 수보다 적으면 남는 호출이 매번 TLS 악수를 다시
                # 한다.** 처음 4로 뒀다가 동시 6으로 재니 중앙값이 227ms → 318ms 로 올랐다
                # (실측 2026-10-06). 동시 턴 하나당 연결 하나이므로 넉넉히 둔다.
                limits=httpx.Limits(max_keepalive_connections=16, max_connections=32),
            )
        return self._http

    async def aclose(self) -> None:
        """열어 둔 연결을 닫는다. 앱 종료나 검사 끝에서 부른다."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _decide(self, user_input: str) -> dict:
        from app.agents.scenario import jev_criteria as spec

        settings = get_settings()
        if not settings.llm_api_key:
            raise RuntimeError("결정 모델 라우터에 LLM_API_KEY 가 없다")
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        if settings.openrouter_app_title:
            headers["X-Title"] = settings.openrouter_app_title

        base = settings.llm_base_url.rstrip("/").removesuffix("/api/v1")
        answer = await self._client().post(
            f"{base}{JEV_DECISIONS_PATH}",
            headers=headers,
            json={
                "model": JEV_MODEL,
                "state": {"user_message": user_input},
                "questions": {
                    "target": {
                        "type": "choice",
                        "instructions": spec.TARGET_INSTRUCTIONS,
                        "criteria": spec.TARGET_CRITERIA,
                    },
                    "route": {
                        "type": "choice",
                        "instructions": spec.ROUTE_INSTRUCTIONS,
                        "criteria": spec.ROUTE_CRITERIA,
                    },
                },
            },
        )
        answer.raise_for_status()
        return answer.json()["answers"]

    async def route(self, user_input: str, context_hint: str = "") -> RouteVerdict:
        # `context_hint` 는 아직 쓰지 않는다. 측정이 문맥 없이 이뤄졌으므로 넣는 것은 재 본
        # 뒤의 일이다 — 문맥을 주면 분류가 달라질 수 있고 그 방향은 측정이 없다.
        try:
            answers = await self._decide(user_input)
            picked = Route(answers["route"]["choice"])
            target = answers["target"]["choice"]
        except Exception as error:  # noqa: BLE001 — 라우터 실패가 턴을 죽이면 안 된다
            logger.warning("[scenario] jev router failed — falling back to authoring: %s", error)
            return RouteVerdict(route=Route.AUTHORING)

        if picked is Route.MODIFY and target != _ASK_WHEN_TARGET_IS_NOT:
            logger.info("[scenario] jev routed to modify but target=%s — asking back", target)
            return RouteVerdict(route=Route.ASK)
        logger.info("[scenario] jev routed to %s (target=%s)", picked.value, target)
        return RouteVerdict(route=picked)
