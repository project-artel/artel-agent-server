"""TC 검증 시도와 사용자 질문을 orchestration에 저장한다."""

import asyncio
from contextlib import contextmanager
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel

from app.config import get_settings


class CaseAttempt(BaseModel):
    id: UUID
    verdict: str | None = None


class OperatorQuestion(BaseModel):
    id: UUID
    state: Literal["WAITING", "ANSWERED", "CANCELLED"]
    answer: str | None = None


class ActiveRunClock:
    """질문 대기만 제한 시간에서 빼고 게임 실행 시간은 계속 센다."""

    def __init__(self) -> None:
        self.started = monotonic()
        self.wait_started: float | None = None
        self.waited = 0.0

    @property
    def elapsed(self) -> float:
        end = self.wait_started if self.wait_started is not None else monotonic()
        return end - self.started - self.waited

    @contextmanager
    def waiting(self):
        if self.wait_started is not None:
            raise RuntimeError("이미 사용자 답변을 기다리고 있습니다.")
        self.wait_started = monotonic()
        try:
            yield
        finally:
            self.waited += monotonic() - self.wait_started
            self.wait_started = None


class QaEvaluationClient:
    def __init__(self, try_id: int) -> None:
        self.try_id = try_id
        self.attempts: dict[int, UUID] = {}
        self.pending_starts: dict[int, UUID] = {}
        self.snapshots: dict[int, dict] = {}
        self.clock = ActiveRunClock()

    def url(self, path: str) -> str:
        root = get_settings().orchestration_base_url
        if not root:
            raise RuntimeError("orchestration_base_url이 설정되지 않았습니다.")
        return f"{root.rstrip('/')}/internal/qa/tries/{self.try_id}/{path}"

    async def post(self, client: httpx.AsyncClient, path: str, payload: dict) -> httpx.Response:
        for retry in range(3):
            try:
                response = await client.post(self.url(path), json=payload)
                response.raise_for_status()
                return response
            except (httpx.TransportError, httpx.HTTPStatusError) as error:
                if retry == 2 or (isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500):
                    raise
                await asyncio.sleep(2 ** retry)
        raise AssertionError("retry loop exhausted")

    async def start(self, case_id: int) -> CaseAttempt:
        if case_id in self.attempts:
            return CaseAttempt(id=self.attempts[case_id])
        attempt_id = self.pending_starts.setdefault(case_id, uuid4())
        async with httpx.AsyncClient(timeout=15) as client:
            response = await self.post(client, "case-attempts/start", {"id": str(attempt_id), "testCaseId": case_id, "caseSnapshot": self.snapshots.get(case_id)})
            attempt = CaseAttempt.model_validate(response.json())
        self.attempts[case_id] = attempt.id
        self.pending_starts.pop(case_id, None)
        return attempt

    async def report(self, case_id: int, verdict: str, reason: str, thought: str, checks, evidence: str | None, citations: list[str] | None = None, used_cheats=None, active_behaviors=None, rejected_knowledge_id_count: int = 0) -> None:
        attempt_id = self.attempts[case_id]
        async with httpx.AsyncClient(timeout=15) as client:
            await self.post(client, f"case-attempts/{attempt_id}/report",
                {"verdict": verdict, "reason": reason, "thought": thought, "checks": checks, "evidence": evidence, "usedKnowledgeIds": citations or [],
                 "usedCheats": used_cheats, "activeBehaviors": active_behaviors,
                 "rejectedKnowledgeIdCount": rejected_knowledge_id_count})
        self.attempts.pop(case_id, None)

    async def ask(self, question: str, cancelled) -> str:
        from app.qa.channel import QaCancelled

        question_id = uuid4()
        with self.clock.waiting():
            async with httpx.AsyncClient(timeout=15) as client:
                # 같은 ID로 재요청하므로 응답 유실 뒤에도 질문은 한 번만 만들어진다.
                while True:
                    if cancelled():
                        raise QaCancelled()
                    try:
                        response = await client.post(self.url("questions"), json={"id": str(question_id), "question": question})
                        response.raise_for_status()
                        break
                    except (httpx.TransportError, httpx.HTTPStatusError) as error:
                        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                            raise
                        await asyncio.sleep(2)
                while True:
                    if cancelled():
                        raise QaCancelled()
                    try:
                        response = await client.get(self.url(f"questions/{question_id}"))
                        response.raise_for_status()
                        result = OperatorQuestion.model_validate(response.json())
                    except (httpx.TransportError, httpx.HTTPStatusError) as error:
                        if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                            raise
                        await asyncio.sleep(2)
                        continue
                    if result.state == "CANCELLED":
                        raise QaCancelled()
                    if result.state == "ANSWERED":
                        return result.answer or ""
                    await asyncio.sleep(1)


async def run_with_active_deadline(awaitable, clock: ActiveRunClock, seconds: float):
    started_at = clock.elapsed
    task = asyncio.create_task(awaitable)
    try:
        while not task.done():
            remaining = seconds - (clock.elapsed - started_at)
            if remaining <= 0:
                raise asyncio.TimeoutError()
            await asyncio.wait({task}, timeout=min(remaining, 0.25))
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
