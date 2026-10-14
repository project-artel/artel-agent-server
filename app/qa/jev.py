# -*- coding: utf-8 -*-
"""Jev decision model 에 선택지 하나를 고르게 하는 호출 (ARTEL-1026).

`typesafe/jev-1.13` 은 OpenRouter `/api/alpha/decisions` 에서 선택지 중 하나를 고르고 글은
쓰지 않는다. 한 번 부르는 값이 약 $0.000115, 지연 중앙값이 240ms 다.

- 시도는 한 번이다. 다시 시도하지 않는다. 호출하는 쪽이 도구 결과를 기다리고 있어서, 재시도는
  agent 의 한 턴을 그만큼 붙잡는다.
- 오류나 `timeout` 이면 `None` 이고 경고만 남긴다. 호출하는 쪽은 `None` 을 "고르지 않았다" 로
  읽고 하던 일을 그대로 한다(fail open).
- 요청 모양은 `app/agents/scenario/router.py` 의 `JevScenarioRouter._decide` 와 같고, 설정은
  `app/qa/relevance.py` 의 `PulseRelevanceJudge._post` 와 같은 곳에서 읽는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)

JEV_MODEL = "typesafe/jev-1.13"
DECISIONS_PATH = "/api/alpha/decisions"
DEFAULT_TIMEOUT_SECONDS = 2.0
_QUESTION = "pick"

# 요청 본문을 받아 응답 JSON 을 돌려준다. 검사가 갈아 끼운다(`relevance.Decide` 와 같은 자리).
Post = Callable[[dict], Awaitable[dict]]

_http: object | None = None


def _client():
    """연결을 재사용한다. 호출마다 새로 열면 매번 TLS 악수를 다시 한다."""
    import httpx

    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
    return _http


async def _post(body: dict) -> dict:
    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("Jev 호출에 LLM_API_KEY 가 없다")
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    if settings.openrouter_site_url:
        headers["HTTP-Referer"] = settings.openrouter_site_url
    if settings.openrouter_app_title:
        headers["X-Title"] = settings.openrouter_app_title
    base = settings.llm_base_url.rstrip("/").removesuffix("/api/v1")
    answer = await _client().post(f"{base}{DECISIONS_PATH}", headers=headers, json=body)
    answer.raise_for_status()
    return answer.json()


def _request(
    instruction: str, options: Sequence[str], descriptions: Mapping[str, str] | None
) -> dict:
    described = descriptions or {}
    return {
        "model": JEV_MODEL,
        "state": {"instruction": instruction},
        "questions": {
            _QUESTION: {
                "type": "choice",
                "instructions": instruction,
                "criteria": {
                    option: {"what": described.get(option, option), "examples": []}
                    for option in options
                },
            }
        },
    }


async def choose(
    instruction: str,
    options: list[str],
    *,
    descriptions: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    post: Post | None = None,
) -> str | None:
    """`options` 중 Jev 가 고른 것. 오류, `timeout`, 선택지 밖의 답이면 `None`.

    `descriptions` 는 선택지 이름 → 설명이다. 없으면 이름이 설명을 겸한다.
    """
    if not options:
        return None
    try:
        response = await asyncio.wait_for(
            (post or _post)(_request(instruction, options, descriptions)), timeout
        )
        choice = response["answers"][_QUESTION]["choice"]
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 — fail open: 고르지 못해도 도구 결과는 나간다
        logger.warning("[QA] jev choose failed: %s: %s", type(error).__name__, error)
        return None
    if choice not in options:
        logger.warning("[QA] jev chose %r, which is not an option", choice)
        return None
    return choice
