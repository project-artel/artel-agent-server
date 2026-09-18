"""이름을 지을 `screen` 의 screen capture 를 모델 앞에 놓는다.

그림이 이 일의 근거 전부에 가깝다. `discriminator` 는 기계 문자열 목록이고, 그것만 보고
지은 이름은 화면을 부르는 대신 목록을 옮겨 적기 쉽다 — `Canvas with continue button
active` 는 restatement 이고 `Title screen` 이 이름이다. 그림이 있으면 그 화면이 무엇인지가
그림에 적혀 있다.

그래도 screen capture 를 못 가져오는 것은 실패가 아니다. `capture_url` 은 서명된 단기
주소라 만료될 수 있고, 아예 없는 요청도 정상이다. 그때는 글만 보고 짓되 **그림이 있었으면
알았을 것은 답하지 않는다** 고 말해 준다(`prompt.NO_CAPTURE_NOTE`).

`screen_verdict/capture.py` 가 이미 푼 문제 둘을 다시 풀지 않고 그 아래 층
(`app/agents/qa/vision.py`)을 같이 쓴다: 못 가져온 것은 예외가 아니라 빈 목록이고, mime 은
확장자가 아니라 바이트가 말한다. 그쪽 함수를 그대로 부르지 않는 것은 그 함수가 제안의 두
화면을 앞뒤로 놓는 일을 하기 때문이다 — 여기는 화면 하나이고 caption 도 다르다.
"""

import base64
import logging

from langchain_core.messages import HumanMessage

from app.agents.qa.vision import CaptureFetchError, download_capture, image_mime_of
from app.qa.envelope import ScreenSelectorScreenRef

logger = logging.getLogger(__name__)


async def fetch_screen_capture(screen: ScreenSelectorScreenRef) -> list[HumanMessage]:
    """이름을 지을 화면의 그림, 없거나 못 가져오면 빈 목록.

    목록으로 돌려주는 것은 프롬프트의 `MessagesPlaceholder` 가 목록을 받기 때문이다. 빈
    목록이 들어가면 프롬프트는 글자 하나 달라지지 않는다.
    """
    if not screen.capture_url:
        return []
    try:
        raw = await download_capture(screen.capture_url)
        mime_type = image_mime_of(raw)
    except CaptureFetchError as error:
        # 이름 짓기를 세우지 않는다. 그림 하나가 안 와서 답이 아예 안 나가면 저쪽은
        # 답을 기다리다 말고, 그 `screen` 은 이름 없이 남는다 — 글만 보고라도 지어 보는
        # 쪽이 낫다.
        logger.warning(
            "[screen-name] the capture for screen %s could not be fetched: %s",
            screen.screen_id,
            error,
        )
        return []

    encoded = base64.b64encode(raw).decode("ascii")
    return [
        HumanMessage(
            content=[
                {
                    "type": "text",
                    "text": f"This is the screen you are naming (screen {screen.screen_id}).",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                },
            ]
        )
    ]
