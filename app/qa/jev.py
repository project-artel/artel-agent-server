"""세 번째 Jev 호출자인 macro runner가 결정 API를 쓰도록 한다.

기존 두 호출자의 코드를 이 client로 옮기는 일은 후속 변경으로 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.llm.decisions import MissingDecisionKey, post_decision

logger = logging.getLogger(__name__)

JEV_MODEL = "typesafe/jev-1.13"
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
    model: Literal["typesafe/jev-1.13"] = JEV_MODEL
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
        try:
            return await post_decision(self._http, body)
        except MissingDecisionKey as error:
            raise JevError(f"Jev requires an OpenRouter key: {error}.") from error

    @staticmethod
    def _log_cost(usage: JevUsage | None) -> None:
        if usage is not None and usage.cost is not None:
            logger.info("[QA] Jev cost $%.6f", usage.cost)
