"""제안이 오면 QA 런 밖에서 답한다 (ARTEL-656).

## 무엇이 격리되는가

이 모듈이 지고 있는 약속은 하나다: **QA 런이 판정을 모른다.** 판정이 느려도, 모델이
형식을 어겨도, 캡처가 만료됐어도, OpenRouter 가 안 돌아와도 런은 하던 것을 계속한다.

그것을 이렇게 만든다:

- 판정은 떨어진 task 에서 돈다. `deliver` 는 task 를 만들고 **바로 돌아온다** — 인입
  프레임을 읽는 loop 가 판정 하나를 기다리면 그동안 `PULSE` 도 `ACTION_RESULT` 도 안
  들어오고, 그것이 곧 런이 서는 것이다
- 판정에는 자기 상한이 붙는다. 모델 client 의 재시도까지 겹치면 한 호출이 십수 분을 살 수
  있고, 그런 task 가 제안마다 하나씩 쌓이면 그것 자체가 사고다
- 동시에 도는 판정 수에 상한이 있다. 넘으면 그 제안을 버린다 — 기본값이 무시라 답하지
  않는 것과 "안 가른다" 가 같은 결과다
- 판정은 QA agent 의 대화에 아무것도 안 남기고, 그 런의 `capture` 예산도 안 쓴다. 그림은
  제안이 싣고 온 주소에서 직접 가져온다
- 세션이 닫히면 남은 task 를 끊는다

## 왜 답을 채널로 내보내는가

봉투의 `sequence` 는 한 세션에 하나뿐이어야 한다. 판정기가 제 카운터를 들면 같은 번호가
두 번 나가고, 그때 소켓 저쪽이 무엇을 하는지는 이쪽이 정하는 것이 아니다. 그래서 프레임을
만드는 자리는 여전히 `QaRunChannel` 하나이고, 이 모듈은 그 메서드를 부른다.
"""

import asyncio
import logging

from pydantic import ValidationError

from app.agents.base import AgentContext
from app.agents.screen_verdict import (
    ScreenVerdictAgent,
    ScreenVerdictError,
    ScreenVerdictRequest,
)
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.usage import set_usage_scope
from app.qa.channel import QaRunChannel
from app.qa.envelope import (
    ScreenSelectorProposalPayload,
    ScreenSelectorVerdictPayload,
)

logger = logging.getLogger(__name__)

# 판정 하나가 살 수 있는 시간. 캡처 둘을 받아 오고 모델을 한 번 부르는 데 드는 것보다
# 넉넉하되, 매달린 호출이 런보다 오래 사는 일이 없게 짧다.
#
# 모델 client 자체가 180초에 재시도까지 하고 형식 위반이면 그 위에서 세 번 다시 물으므로,
# 이 상한이 없으면 한 판정이 십수 분을 산다. 넘긴 판정은 답 없이 끝난다 — 기본값이
# 무시라 그것이 지도를 종전대로 두는 결과다.
VERDICT_TIMEOUT_SECONDS = 180.0

# 동시에 도는 판정 수.
#
# 저쪽은 같은 `(scene, selector)` 를 두 번 묻지 않지만 서로 다른 대상은 잇달아 물을 수
# 있고, 처음 보는 `scene` 에 들어선 순간이 정확히 그 순간이다. 상한이 없으면 모델 호출이
# 그 순간에 몰린다.
MAX_VERDICTS_IN_FLIGHT = 4

# 답을 못 낸 이유를 `note` 에 적는다. 프레임은 `qa_log` 에 남으므로, 이 문장이 나중에
# "이 후보들은 왜 목록에 없나" 에 답하는 유일한 기록이다.
_FAILED_NOTE = "This proposal could not be judged ({reason}), so it is answered with no entries rather than with invented ones."

# `note` 에 실을 사유의 길이 상한. 형식을 어긴 모델의 답 전문이 그대로 들어올 수 있고,
# 그것은 사유가 아니라 페이로드다 — 프레임 하나가 `qa_log` 한 행이므로 잘라 싣는다.
_MAX_NOTE_REASON = 400


def _short(reason: str) -> str:
    text = " ".join(reason.split())
    if len(text) <= _MAX_NOTE_REASON:
        return text
    return f"{text[:_MAX_NOTE_REASON]}…"


class ScreenSelectorAdjudicator:
    """제안을 받아 QA 런 밖에서 답하는 자리. 한 시나리오에 하나.

    ``agent`` 는 테스트가 실제 모델 대신 정해진 답을 물릴 수 있게 열어 둔 자리다.
    """

    def __init__(
        self,
        model: LLMModel = DEFAULT_MODEL,
        agent: ScreenVerdictAgent | None = None,
        timeout: float = VERDICT_TIMEOUT_SECONDS,
        max_in_flight: int = MAX_VERDICTS_IN_FLIGHT,
    ) -> None:
        self._model = model
        self._agent = agent or ScreenVerdictAgent()
        self._timeout = timeout
        self._max_in_flight = max_in_flight
        # asyncio 는 task 를 약참조로만 들고 있어, 여기서 안 잡으면 도는 중에 수거될 수
        # 있다. 세션이 닫힐 때 끊을 대상이기도 하다.
        self._running: set[asyncio.Task] = set()

    def answer_later(self, channel: QaRunChannel, raw: dict) -> None:
        """제안 하나에 대한 답을 예약한다. **아무것도 기다리지 않고 바로 돌아온다.**

        여기서 예외를 내보내지 않는다. 부르는 쪽은 인입 프레임을 읽는 loop 이고, 그
        loop 가 판정 때문에 서면 QA 런이 화면도 액션 결과도 못 받는다.
        """
        proposal_id = raw.get("messageId")
        if not isinstance(proposal_id, str) or not proposal_id:
            # 답을 붙일 자리가 없다. 저쪽은 `correlationId` 나 `proposal_id` 로 제안을
            # 찾으므로, 그 값이 없으면 보내 봐야 미아가 된다.
            logger.warning("[screen-verdict] a proposal arrived without a messageId")
            return

        if len(self._running) >= self._max_in_flight:
            logger.warning(
                "[screen-verdict] %d verdicts already in flight; proposal %s is dropped",
                len(self._running),
                proposal_id,
            )
            return

        try:
            proposal = ScreenSelectorProposalPayload.model_validate(
                raw.get("payload") or {}
            )
        except ValidationError as error:
            logger.warning("[screen-verdict] unreadable proposal %s: %s", proposal_id, error)
            return

        if not proposal.candidates:
            # 물어본 것이 없는 제안. 모델을 부를 이유가 없고, 부르면 후보 없는 답을
            # 짓게 만드는 것이 전부다.
            return

        task = asyncio.create_task(self._answer(channel, proposal_id, proposal))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    def close(self) -> None:
        """남은 판정을 끊는다. 세션이 닫힐 때 부른다.

        보낼 소켓이 사라진 뒤에도 모델 호출이 계속 도는 것을 막는다. 끊긴 판정은 답
        없이 끝나고, 그것은 지도를 종전대로 두는 결과다.
        """
        for task in list(self._running):
            task.cancel()
        self._running.clear()

    async def _answer(
        self,
        channel: QaRunChannel,
        proposal_id: str,
        proposal: ScreenSelectorProposalPayload,
    ) -> None:
        # 이 task 의 모델 호출이 어느 try 때문에 났는지. contextvar 는 task 를 타므로
        # 여기서 세우면 QA 런의 것과 섞이지 않는다.
        set_usage_scope("QA_RUN", channel.qa_try_id)
        try:
            payload = await asyncio.wait_for(
                self._judge(channel, proposal_id, proposal), timeout=self._timeout
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            payload = _failed(proposal_id, f"it took longer than {self._timeout:.0f}s")
        except Exception as error:  # noqa: BLE001 - 판정은 런을 끝낼 수 없다
            logger.warning(
                "[screen-verdict] proposal %s could not be judged: %s",
                proposal_id,
                error,
                exc_info=True,
            )
            payload = _failed(proposal_id, str(error))

        if channel.cancelled:
            # 런이 끝나는 중이면 보낼 곳이 없다. 소켓은 곧 닫히고 저쪽은 이 try 를 이미
            # 접었을 수 있다.
            return
        try:
            await channel.answer_screen_selector_proposal(payload, proposal_id)
        except Exception as error:  # noqa: BLE001 - 소켓이 이미 닫혔을 수 있다
            logger.warning(
                "[screen-verdict] the verdict for %s could not be sent: %s",
                proposal_id,
                error,
            )

    async def _judge(
        self,
        channel: QaRunChannel,
        proposal_id: str,
        proposal: ScreenSelectorProposalPayload,
    ) -> ScreenSelectorVerdictPayload:
        context = AgentContext(
            session_id=f"screen-verdict-{proposal_id}",
            metadata={"qa_try_id": channel.qa_try_id, "scene": proposal.scene.name},
        )
        try:
            verdict = await self._agent.run(
                ScreenVerdictRequest(proposal=proposal, model=self._model), context
            )
        except ScreenVerdictError as error:
            return _failed(proposal_id, str(error))

        note = verdict.note
        if verdict.dropped:
            dropped = "; ".join(
                f"{item.entry.match} `{item.entry.pattern}`: {item.reason}"
                for item in verdict.dropped
            )
            # 버린 것을 답에 적는다. 버림이 조용하면 "왜 이 후보만 목록에 없나" 를
            # 되짚을 자리가 어디에도 없다.
            unusable = _short(
                f"{len(verdict.dropped)} entry/entries were unusable — {dropped}"
            )
            note = f"{note} {unusable}" if note else unusable

        logger.info(
            "[screen-verdict] proposal %s answered with %d entry/entries (%d dropped)",
            proposal_id,
            len(verdict.entries),
            len(verdict.dropped),
        )
        return ScreenSelectorVerdictPayload(
            proposal_id=proposal_id, entries=verdict.entries, note=note
        )


def _failed(proposal_id: str, reason: str) -> ScreenSelectorVerdictPayload:
    """항목 없는 답. 형식 위반과 실패가 같은 모양으로 나가는 자리."""
    return ScreenSelectorVerdictPayload(
        proposal_id=proposal_id,
        entries=[],
        note=_FAILED_NOTE.format(reason=_short(reason)),
    )
