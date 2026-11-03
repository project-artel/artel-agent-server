"""PE 엔진의 화면 한 장. planner 와 evaluator 가 그림으로 화면을 본다(knob `vision`).

tool loop 의 자동 캡처(`app/agents/qa/vision.py`)와 같은 길이다: SDK 에 `capture_screen` 을 보내면 게임이 JPEG 를 저장소에
올리고 주소를 돌려주며, 그것을 받아 base64 로 싣는다. 실패해도 런은 멈추지 않는다 — 그림 없이 값으로 판단한다.

Jev 에게는 보내지 않는다. 2026-10-30 실측에서 라벨이 있는 화면은 그림을 더해도 맞힌 수가 같고 비용만 1.7배였다.
"""

import logging
from dataclasses import dataclass

from app.agents.qa.tools.state import capture_from_action_result
from app.agents.qa.vision import CaptureFetchError, fetch_capture
from app.qa.channel import QaRunChannel
from app.qa.envelope import JsonRpcAction

logger = logging.getLogger(__name__)

# 게임이 캡처를 찍어 올리는 데 기다리는 시간. tool loop 의 자동 캡처와 같은 크기다.
CAPTURE_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class ScreenImage:
    """화면 한 장: `capture_id` 는 로그에서 그림을 다시 찾는 열쇠다."""

    capture_id: str
    mime_type: str
    data: str

    def block(self) -> dict:
        """모델 메시지의 이미지 content block."""
        return {"type": "image_url", "image_url": {"url": f"data:{self.mime_type};base64,{self.data}"}}


async def capture_screen(channel: QaRunChannel, why: str, step: int | None = None) -> ScreenImage | None:
    """지금 화면 한 장, 못 찍었으면 `None`. `why` 는 SDK 로 나가는 ACTION 의 설명이다."""
    try:
        result = await channel.dispatch_actions(
            [JsonRpcAction(id=1, method="capture_screen", params=[])], why, step, timeout=CAPTURE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        logger.warning("[QA] screen capture timed out (%s)", why)
        return None
    if result is None or not result.results:
        logger.warning("[QA] the game did not answer the screen capture (%s)", why)
        return None
    capture = capture_from_action_result(result.results[0], "the screen")
    if capture is None:
        logger.warning("[QA] screen capture produced no image (%s): %s", why, result.results[0].error)
        return None
    try:
        data = await fetch_capture(capture.url, capture.mime_type)
    except CaptureFetchError as error:
        logger.warning("[QA] screen capture %s could not be fetched: %s", capture.capture_id, error)
        return None
    return ScreenImage(capture.capture_id, capture.mime_type, data)
