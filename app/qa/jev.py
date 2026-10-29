"""macro runner 가 결정 API 를 쓰도록 한다.

요청은 `app/llm/decision.py` 의 `decide` 로 보낸다. 관리자가 결정 model 로 Jev 가 아닌
model 을 골랐으면 그 model 이 답하고, 실패는 그대로 `JevError` 다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.llm.decision import JEV_MODEL, decide

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20.0


class JevError(Exception):
    """Jev 호출이나 응답 검증이 실패했음을 알린다."""


class JevChoiceCriterion(BaseModel):
    what: str


class JevChoiceQuestion(BaseModel):
    type: Literal["choice"] = "choice"
    instructions: str
    criteria: dict[str, JevChoiceCriterion]


class JevNoulQuestion(BaseModel):
    type: Literal["noul"] = "noul"
    instructions: str
    criteria: dict[str, str]


class JevRequest(BaseModel):
    # 실제로 보내는 model 은 `decide` 가 정한다. 이 값은 Jev 로 보낼 때 덮어쓰인다.
    model: str = JEV_MODEL
    state: dict[str, Any]
    questions: dict[str, JevChoiceQuestion | JevNoulQuestion]


class JevChoiceAnswer(BaseModel):
    choice: str


class JevNoulAnswer(BaseModel):
    noul: float = Field(ge=0.0, le=1.0)


class JevUsage(BaseModel):
    cost: float | None = None


class JevChoiceResponse(BaseModel):
    answers: dict[str, JevChoiceAnswer]
    usage: JevUsage | None = None


class JevNoulResponse(BaseModel):
    answers: dict[str, JevNoulAnswer]
    usage: JevUsage | None = None


class JevClient:
    def __init__(self, post: Callable[[dict], Awaitable[dict]] | None = None) -> None:
        self._post_call = post or self._post
        self._http: httpx.AsyncClient | None = None

    async def choose(
        self, instructions: str, options: dict[str, str], state: dict[str, Any]
    ) -> str:
        request = JevRequest(
            state=state,
            questions={
                "answer": JevChoiceQuestion(
                    instructions=instructions,
                    criteria={
                        option: JevChoiceCriterion(what=description)
                        for option, description in options.items()
                    },
                )
            },
        )
        response = await self._send(request)
        try:
            answer = JevChoiceResponse.model_validate(response)
            choice = answer.answers["answer"].choice
        except (ValidationError, KeyError) as error:
            raise JevError("Jev choice response is missing a valid answers.answer.choice.") from error
        if choice not in options:
            raise JevError(f"Jev chose an unknown option: {choice!r}.")
        self._log_cost(answer.usage)
        return choice

    async def probability(
        self, instructions: str, state: dict[str, Any], true_means: str, false_means: str
    ) -> float:
        request = JevRequest(
            state=state,
            questions={
                "answer": JevNoulQuestion(
                    instructions=instructions,
                    criteria={"true": true_means, "false": false_means},
                )
            },
        )
        response = await self._send(request)
        try:
            answer = JevNoulResponse.model_validate(response)
            probability = answer.answers["answer"].noul
        except (ValidationError, KeyError) as error:
            raise JevError("Jev noul response is missing a valid probability in [0, 1].") from error
        self._log_cost(answer.usage)
        return probability

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send(self, request: JevRequest) -> dict:
        try:
            return await self._post_call(request.model_dump())
        except asyncio.CancelledError:
            raise
        except JevError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException) as error:
            raise JevError(f"Jev request timed out: {error}.") from error
        except httpx.HTTPStatusError as error:
            raise JevError(f"Jev returned HTTP {error.response.status_code}.") from error
        except httpx.HTTPError as error:
            raise JevError(f"Jev HTTP request failed: {error}.") from error
        except Exception as error:  # noqa: BLE001 — 호출자의 실패 타입을 하나로 유지한다.
            raise JevError(f"Jev request failed: {error}.") from error

    async def _post(self, body: dict) -> dict:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return await decide(self._http, body)

    @staticmethod
    def _log_cost(usage: JevUsage | None) -> None:
        if usage is not None and usage.cost is not None:
            logger.info("[QA] Jev cost $%.6f", usage.cost)
