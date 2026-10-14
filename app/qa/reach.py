# -*- coding: utf-8 -*-
"""FREE 시나리오의 실패 판정이 "아직 도달하지 못했다" 인지 Jev 결정 모델로 가린다 (ARTEL-1029).

2026-10-14 로컬 run(qa_try 3, Claude Haiku 4.5)에서 agent 가 context 압축 뒤 "time is limited"
라고 스스로 판단하고, TC 5개를 시도하지 않은 채 실패로 보고했다. 메시지는 "전투 진행 중",
"추가 시간 필요", "아직 스테이지를 클리어하지 않았으므로 테스트할 수 없다" 였다. 그 run 에는
마감도 호출 상한도 걸리지 않았다.

prompt 로만 막으면 model 이 지키기를 기대하는 것뿐이라, `report_case(passed=false)` 를 받을
때 TC 의 test_step·expected 와 agent 의 message 를 Jev 에 주고 "해 보고 실패했는가, 아직 거기까지
안 갔는가" 를 묻는다.

- 실패한 판정만 묻는다. 통과 판정은 미도달일 수 없다.
- 호출이 실패하면 None 을 돌려준다. 호출부는 판정을 그대로 기록한다(fail open).
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from app.qa.relevance import RELEVANCE_MODEL, TIMEOUT_SECONDS, post_decision

logger = logging.getLogger(__name__)

# 이 확률 이상으로 미도달이면 판정을 기록하지 않고 돌려보낸다.
UNREACHED_THRESHOLD = 0.8
# 같은 TC 를 이만큼 돌려보냈으면 다음 보고는 기록한다. agent 가 정말 갈 수 없는 TC 에서
# 런이 멈추지 않게 하는 상한이다.
MAX_UNREACHED_REFUSALS = 2

_CRITERIA = {
    "true": "the tester carried out the test step and the expected result did not happen, "
    "or the precondition can no longer be reached",
    "false": "the tester has not reached or not finished the test step yet",
}

Decide = Callable[[dict], Awaitable[dict]]


def _instructions(test_step: str, expected: str, message: str) -> str:
    return (
        "A game QA tester reported a test case as FAILED.\n"
        f"Test step: {test_step}\n"
        f"Expected result: {expected}\n"
        f"Tester's report: {message}\n"
        "Did the tester actually carry out the test step and see that the expected result "
        "did not happen (or find the precondition permanently unreachable), rather than "
        "stopping before reaching or finishing the test step?"
    )


class UnreachedJudge:
    """`decide` 는 요청 본문을 받아 응답 JSON 을 돌려준다. 검사가 갈아 끼운다."""

    def __init__(self, decide: Decide | None = None) -> None:
        self._decide: Decide = decide or self._post
        self._http: object | None = None

    async def unreached_probability(
        self, test_step: str, expected: str, message: str
    ) -> float | None:
        """미도달일 확률. 판단하지 못하면 None."""
        body = {
            "model": RELEVANCE_MODEL,
            "questions": {
                "verdict": {
                    "type": "noul",
                    "instructions": _instructions(test_step, expected, message),
                    "criteria": dict(_CRITERIA),
                }
            },
        }
        try:
            response = await self._decide(body)
            attempted = float(response["answers"]["verdict"]["noul"])
        except Exception as error:  # noqa: BLE001 — fail open: 판정을 그대로 기록한다
            logger.warning("[QA] unreached judge failed: %s", error)
            return None
        return 1.0 - attempted

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _post(self, body: dict) -> dict:
        import httpx

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        return await post_decision(self._http, body)
