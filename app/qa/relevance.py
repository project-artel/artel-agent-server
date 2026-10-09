# -*- coding: utf-8 -*-
"""pulse view 가 숨길 cosmetic member 를 결정 모델로 가린다 (ARTEL-958).

전투 중 pulse 변화의 98% 가 `SlimeAnimator::spriteRenderer` 하나였다. animation sprite,
tween, camera shake 같은 cosmetic member 가 QA agent 의 view 를 채우면 정작 봐야 할
`Enemy.Hp` 가 묻힌다. 그래서 OpenRouter 의 Jev 결정 모델에 member **type** 마다 "QA 가
봐야 할 gameplay state 인가" 를 묻고, 확률을 `PulseMemory.relevance` 에 남겨 view 가
거르게 한다.

- type key(`declaring type::member`) 로 cache 한다. 카드 clone 20장이 같은 member 를 가져도
  질문은 하나다.
- run 당 한 번만 판정한다. scenario step 마다 다르게 판정하는 일은 하지 않는다.
- 실패하면 `relevance` 를 비워 둔다. 비어 있으면 view 가 전부 보여 주므로 fail open 이다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from app.config import get_settings
from app.qa.pulse import PulseMemory

logger = logging.getLogger(__name__)

RELEVANCE_MODEL = "typesafe/jev-1.13"
DECISIONS_PATH = "/api/alpha/decisions"
# 질문 하나가 약 110 token 으로 측정됐다. 40개면 32k context 의 15% 도 안 쓴다.
QUESTIONS_PER_REQUEST = 40
MAX_ATTEMPTS = 2
VALUE_CHARS = 80
TIMEOUT_SECONDS = 20.0

_CRITERIA = {"true": "gameplay state", "false": "cosmetic or internal noise"}

Decide = Callable[[dict], Awaitable[dict]]


class MemberSample(BaseModel):
    type_key: str
    object: str
    component: str
    member: str
    value: str


def _render_value(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= VALUE_CHARS else text[: VALUE_CHARS - 1] + "…"


def _instructions(sample: MemberSample) -> str:
    return (
        f"In object {sample.object}, is the field {sample.component}.{sample.member} "
        f"(current value {sample.value}) game state a QA tester must watch to play or judge "
        "the game, rather than animation, visual effect, or decoration?"
    )


class PulseRelevanceJudge:
    """`decide` 는 요청 본문을 받아 응답 JSON 을 돌려준다. 검사가 갈아 끼운다."""

    def __init__(self, decide: Decide | None = None) -> None:
        self._decide: Decide = decide or self._post
        self._http: object | None = None
        self._task: asyncio.Task | None = None
        self._attempts: dict[str, int] = {}
        self._inflight: set[str] = set()
        self._memory: PulseMemory | None = None

    # ── 표본 ────────────────────────────────────────────────────────────────

    def samples(self, memory: PulseMemory) -> list[MemberSample]:
        chosen: dict[str, tuple[bool, MemberSample]] = {}
        for held in memory.held.values():
            live = held.live
            where = held.selector or held.path or ""
            for member in held.members.values():
                key = member.type_key
                if (
                    key in memory.relevance
                    or key in self._inflight
                    or self._attempts.get(key, 0) >= MAX_ATTEMPTS
                ):
                    continue
                if key in chosen and (chosen[key][0] or not live):
                    continue
                component = (member.on or "").rsplit(".", 1)[-1]
                chosen[key] = (
                    live,
                    MemberSample(
                        type_key=key,
                        object=where,
                        component=component,
                        member=member.member or "",
                        value=_render_value(member.value),
                    ),
                )
        return [chosen[key][1] for key in sorted(chosen)]

    # ── 실행 ────────────────────────────────────────────────────────────────

    def notice(self, memory: PulseMemory) -> None:
        """판독이 적용된 직후 부른다. 새 type 이 있고 진행 중인 task 가 없으면 하나 시작한다."""
        try:
            self._memory = memory
            if self._task is not None and not self._task.done():
                return
            if not self.samples(memory):
                return
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run())
        except RuntimeError:
            return  # 돌고 있는 loop 가 없다
        except Exception as error:  # noqa: BLE001 — 판정 실패가 판독 처리를 죽이면 안 된다
            logger.warning("[QA] pulse relevance notice failed: %s", error)

    async def _run(self) -> None:
        total_cost = 0.0
        try:
            while True:
                memory = self._memory
                pending = self.samples(memory)
                if not pending:
                    break
                for start in range(0, len(pending), QUESTIONS_PER_REQUEST):
                    total_cost += await self._judge_chunk(
                        memory, pending[start : start + QUESTIONS_PER_REQUEST]
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            logger.warning("[QA] pulse relevance failed: %s", error)
        finally:
            self._inflight.clear()
        if total_cost:
            logger.info("[QA] pulse relevance cost $%.6f", total_cost)

    async def _judge_chunk(self, memory: PulseMemory, chunk: list[MemberSample]) -> float:
        names = {f"m{index}": sample for index, sample in enumerate(chunk)}
        self._inflight.update(sample.type_key for sample in chunk)
        body = {
            "model": RELEVANCE_MODEL,
            "state": {"scene": memory.scene},
            "questions": {
                name: {
                    "type": "noul",
                    "instructions": _instructions(sample),
                    "criteria": dict(_CRITERIA),
                }
                for name, sample in names.items()
            },
        }
        try:
            response = await self._decide(body)
            answers = response["answers"]
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — fail open: relevance 를 비워 둔다
            logger.warning("[QA] pulse relevance failed for %d types: %s", len(chunk), error)
            for sample in chunk:
                self._attempts[sample.type_key] = self._attempts.get(sample.type_key, 0) + 1
            self._inflight.difference_update(sample.type_key for sample in chunk)
            return 0.0

        for name, sample in names.items():
            try:
                memory.relevance[sample.type_key] = float(answers[name]["noul"])
            except (KeyError, TypeError, ValueError):
                self._attempts[sample.type_key] = self._attempts.get(sample.type_key, 0) + 1
        self._inflight.difference_update(sample.type_key for sample in chunk)
        try:
            return float((response.get("usage") or {}).get("cost") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    async def drain(self) -> None:
        """돌고 있는 판정이 끝날 때까지 기다린다. 판정의 실패는 `_run` 이 이미 삼켰다."""
        if self._task is not None:
            await self._task

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── 기본 호출 ───────────────────────────────────────────────────────────

    def _client(self):
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return self._http

    async def _post(self, body: dict) -> dict:
        settings = get_settings()
        if not settings.llm_api_key:
            raise RuntimeError("pulse relevance 에 LLM_API_KEY 가 없다")
        headers = {
            "Authorization": f"Bearer {settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        if settings.openrouter_site_url:
            headers["HTTP-Referer"] = settings.openrouter_site_url
        if settings.openrouter_app_title:
            headers["X-Title"] = settings.openrouter_app_title
        base = settings.llm_base_url.rstrip("/").removesuffix("/api/v1")
        answer = await self._client().post(f"{base}{DECISIONS_PATH}", headers=headers, json=body)
        answer.raise_for_status()
        return answer.json()
