"""Jev 결정 API(`/api/alpha/decisions`)를 부르는 한 자리(ARTEL-1071).

저작의 결정 모델 라우터와 QA 의 판정 · 관련도 · 도달 판단이 같은 API 를 부른다. 셋이 각자 key 를
읽다가, key 의 출처가 orchestration 으로 옮겨 갈 때(#224) 셋 다 옛 설정(`llm_api_key`)에 남아 매번
실패했다. key 와 header 를 여기 하나에서 만든다.

key 는 다른 model 호출과 같이 orchestration 에서 받는다(`app/llm/api_key.py`). 받는 일은 동기 HTTP 라
이벤트 루프를 막지 않게 스레드로 넘긴다. 401 이면 관리자가 key 를 바꿨을 수 있으므로 캐시를 버린다.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.config import get_settings
from app.llm.api_key import UNAUTHORIZED, get_llm_api_key_provider

DECISIONS_PATH = "/api/alpha/decisions"


class MissingDecisionKey(RuntimeError):
    """orchestration 에 OpenRouter key 가 없다."""


async def post_decision(http: Any, body: dict) -> dict:
    """[body] 를 결정 API 에 보내고 응답 JSON 을 돌려준다. HTTP 오류는 `raise_for_status` 그대로 올린다."""
    keys = get_llm_api_key_provider()
    api_key = await asyncio.to_thread(keys.get_openrouter_key)
    if not api_key:
        raise MissingDecisionKey("Jev 결정 API 에 쓸 OpenRouter key 가 orchestration 에 없다")
    settings = get_settings()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_title:
        headers["X-Title"] = settings.openrouter_app_title
    base = settings.llm_base_url.rstrip("/").removesuffix("/api/v1")
    answer = await http.post(f"{base}{DECISIONS_PATH}", headers=headers, json=body)
    if answer.status_code == UNAUTHORIZED:
        keys.invalidate()
    answer.raise_for_status()
    return answer.json()
