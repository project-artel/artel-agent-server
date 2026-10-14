"""이름을 물어보면 QA 런 밖에서 답한다 (ARTEL-909).

## 무엇이 격리되는가

`app/qa/screen_verdict.py` 가 지고 있는 약속을 그대로 진다: **QA 런이 이름 짓기를
모른다.** 이름이 늦게 나와도, 모델이 형식을 어겨도, screen capture 가 만료됐어도 런은
하던 것을 계속한다.

- 이름 짓기는 떨어진 task 에서 돈다. `answer_later` 는 task 를 만들고 **바로 돌아온다** —
  인입 frame 을 읽는 loop 가 이름 하나를 기다리면 그동안 `PULSE` 도 `ACTION_RESULT` 도
  안 들어오고, 그것이 곧 런이 서는 것이다
- 이름 짓기에는 자기 상한이 붙는다
- 동시에 도는 수에 상한이 있다. 넘으면 그 요청을 버린다 — 답하지 않는 것과 이름 없이
  답하는 것이 지도에 같은 결과를 남긴다
- QA agent 의 대화에 아무것도 안 남기고, 그 런의 `capture` 예산도 안 쓴다. 그림은 요청이
  싣고 온 주소에서 직접 가져온다
- 세션이 닫히면 남은 task 를 끊는다

## 왜 답을 채널로 내보내는가

봉투의 `sequence` 는 한 세션에 하나뿐이어야 한다. 이쪽이 제 카운터를 들면 같은 번호가 두
번 나가고, 그때 소켓 저쪽이 무엇을 하는지는 이쪽이 정하는 것이 아니다. 그래서 frame 을
만드는 자리는 여전히 `QaRunChannel` 하나이고, 이 모듈은 그 메서드를 부른다.
"""

import asyncio
import logging

from pydantic import ValidationError

from app.agents.base import AgentContext
from app.agents.screen_name import ScreenNameAgent, ScreenNameError, ScreenNameRequest
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.usage import set_usage_scope
from app.qa.channel import QaRunChannel
from app.qa.envelope import ScreenNamePayload, ScreenNameRequestPayload

logger = logging.getLogger(__name__)

# 이름 하나가 살 수 있는 시간.
#
# 판정의 180초보다 짧다. 그림 하나를 받아 오고 모델을 한 번 부르는 일이라 판정보다 적게
# 드는 것이 첫째 이유이고, 둘째는 못 지었을 때의 대가다 — 판정은 답을 못 내면 그 후보들이
# 영영 다시 안 물어지지만, 이름은 없으면 `screen` 이 이름 없이 남을 뿐이다. 모델 client
# 자체가 180초를 기다리므로 이 상한은 한 번의 호출을 끝까지 기다려 주지 않는다. 그렇게
# 두는 것이 여기서는 맞는 선택이다.
NAME_TIMEOUT_SECONDS = 120.0

# 동시에 도는 이름 짓기 수.
#
# 처음 보는 `scene` 에 들어서면 화면이 잇달아 굳고, 저쪽은 굳는 행마다 물어본다. 상한이
# 없으면 모델 호출이 그 순간에 몰린다.
MAX_NAMES_IN_FLIGHT = 4

# 이름을 못 지은 이유를 `note` 에 적는다. frame 이 `qa_log` 에 남으므로, 이 문장이 나중에
# "이 `screen` 은 왜 이름이 없나" 에 답하는 유일한 기록이다.
_FAILED_NOTE = "This screen could not be named ({reason}), so it is answered with no name rather than with an invented one."

# 근거가 하나도 없어 모델을 부르지 않은 경우.
_NO_EVIDENCE_NOTE = (
    "This screen came with no capture and an empty discriminator, so there was "
    "nothing to name it from."
)

# `note` 에 실을 사유의 길이 상한. 형식을 어긴 모델의 답 전문이 그대로 들어올 수 있고,
# 그것은 사유가 아니라 payload 다 — frame 하나가 `qa_log` 한 행이므로 잘라 싣는다.
_MAX_NOTE_REASON = 400


def _short(reason: str) -> str:
    text = " ".join(reason.split())
    if len(text) <= _MAX_NOTE_REASON:
        return text
    return f"{text[:_MAX_NOTE_REASON]}…"


class ScreenNamer:
    """이름 요청을 받아 QA 런 밖에서 답하는 자리. 한 시나리오에 하나.

    ``agent`` 는 테스트가 실제 모델 대신 정해진 답을 물릴 수 있게 열어 둔 자리다.
    """

    def __init__(
        self,
        model: LLMModel = DEFAULT_MODEL,
        agent: ScreenNameAgent | None = None,
        timeout: float = NAME_TIMEOUT_SECONDS,
        max_in_flight: int = MAX_NAMES_IN_FLIGHT,
    ) -> None:
        self._model = model
        self._agent = agent or ScreenNameAgent()
        self._timeout = timeout
        self._max_in_flight = max_in_flight
        # asyncio 는 task 를 약참조로만 들고 있어, 여기서 안 잡으면 도는 중에 수거될 수
        # 있다. 세션이 닫힐 때 끊을 대상이기도 하다.
        self._running: set[asyncio.Task] = set()

    def answer_later(self, channel: QaRunChannel, raw: dict) -> None:
        """요청 하나에 대한 답을 예약한다. **아무것도 기다리지 않고 바로 돌아온다.**

        여기서 예외를 내보내지 않는다. 부르는 쪽은 인입 frame 을 읽는 loop 이고, 그
        loop 가 이름 하나 때문에 서면 QA 런이 화면도 액션 결과도 못 받는다.
        """
        try:
            request = ScreenNameRequestPayload.model_validate(raw.get("payload") or {})
        except ValidationError as error:
            logger.warning("[screen-name] unreadable request: %s", error)
            return

        # payload 의 `request_id` 를 먼저 본다. 계약이 답의 `correlation_id` 로 정한 값이
        # 그것이고, 봉투의 `messageId` 는 그 값이 비었을 때의 대비책이다.
        request_id = request.request_id or raw.get("messageId")
        if not isinstance(request_id, str) or not request_id:
            # 답을 붙일 자리가 없다. 저쪽은 이 값으로 요청을 찾으므로, 없으면 보내 봐야
            # 미아가 된다.
            logger.warning("[screen-name] a request arrived with no request_id")
            return

        if len(self._running) >= self._max_in_flight:
            logger.warning(
                "[screen-name] %d names already in flight; request %s is dropped",
                len(self._running),
                request_id,
            )
            return

        if not request.screen.capture_url and not request.screen.discriminator:
            # 근거가 없다. 그림도 없고 `discriminator` 도 비었으면 남는 것은 `scene`
            # 이름뿐인데, 그것으로 지은 이름은 그 `scene` 의 모든 화면에 똑같이 붙는다 —
            # 번호를 대신하지 못한다. 모델을 부르면 그 이름을 짓게 만드는 것이 전부다.
            self._answer_now(channel, request_id, None, _NO_EVIDENCE_NOTE)
            return

        task = asyncio.create_task(self._answer(channel, request_id, request))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    def close(self) -> None:
        """남은 이름 짓기를 끊는다. 세션이 닫힐 때 부른다.

        보낼 소켓이 사라진 뒤에도 모델 호출이 계속 도는 것을 막는다. 끊긴 요청은 답 없이
        끝나고, 그 `screen` 은 이름 없이 남는다.
        """
        for task in list(self._running):
            task.cancel()
        self._running.clear()

    def _answer_now(
        self, channel: QaRunChannel, request_id: str, name: str | None, note: str | None
    ) -> None:
        """모델을 안 부르고 답한다. 이것도 기다리지 않는다 — 보내는 것만 task 로 넘긴다."""
        payload = ScreenNamePayload(request_id=request_id, name=name, note=note)
        task = asyncio.create_task(self._send(channel, request_id, payload))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _answer(
        self,
        channel: QaRunChannel,
        request_id: str,
        request: ScreenNameRequestPayload,
    ) -> None:
        # 이 task 의 모델 호출이 어느 try 때문에 났는지. contextvar 는 task 를 타므로
        # 여기서 세우면 QA 런의 것과 섞이지 않는다.
        set_usage_scope("QA_RUN", channel.qa_try_id)
        try:
            payload = await asyncio.wait_for(
                self._name(channel, request_id, request), timeout=self._timeout
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            payload = _failed(request_id, f"it took longer than {self._timeout:.0f}s")
        except Exception as error:  # noqa: BLE001 - 이름 짓기는 런을 끝낼 수 없다
            logger.warning(
                "[screen-name] request %s could not be named: %s",
                request_id,
                error,
                exc_info=True,
            )
            payload = _failed(request_id, str(error))

        await self._send(channel, request_id, payload)

    async def _send(
        self, channel: QaRunChannel, request_id: str, payload: ScreenNamePayload
    ) -> None:
        if channel.cancelled:
            # 런이 끝나는 중이면 보낼 곳이 없다. 소켓은 곧 닫히고 저쪽은 이 try 를 이미
            # 접었을 수 있다.
            return
        try:
            await channel.answer_screen_name_request(payload, request_id)
        except Exception as error:  # noqa: BLE001 - 소켓이 이미 닫혔을 수 있다
            logger.warning(
                "[screen-name] the name for %s could not be sent: %s", request_id, error
            )

    async def _name(
        self,
        channel: QaRunChannel,
        request_id: str,
        request: ScreenNameRequestPayload,
    ) -> ScreenNamePayload:
        context = AgentContext(
            session_id=f"screen-name-{request_id}",
            metadata={"qa_try_id": channel.qa_try_id, "scene": request.scene.name},
        )
        try:
            named = await self._agent.run(
                ScreenNameRequest(
                    screen=request.screen, scene=request.scene, model=self._model
                ),
                context,
            )
        except ScreenNameError as error:
            return _failed(request_id, str(error))

        note = named.note
        if named.dropped is not None:
            # 버린 것을 답에 적는다. 버림이 조용하면 "왜 이 화면만 이름이 없나" 를 되짚을
            # 자리가 어디에도 없다.
            unusable = _short(f"the name was not usable — {named.dropped}")
            note = f"{note} {unusable}" if note else unusable

        logger.info(
            "[screen-name] request %s answered with %s",
            request_id,
            f"{named.name!r}" if named.name else "no name",
        )
        return ScreenNamePayload(request_id=request_id, name=named.name, note=note)


def _failed(request_id: str, reason: str) -> ScreenNamePayload:
    """이름 없는 답. 형식 위반과 실패가 같은 모양으로 나가는 자리."""
    return ScreenNamePayload(
        request_id=request_id, name=None, note=_FAILED_NOTE.format(reason=_short(reason))
    )
