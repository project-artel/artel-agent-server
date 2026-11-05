"""제안에 실려 온 캡처를 모델 앞에 놓는다.

캡처가 이 판정의 절반이다. "화면에서 실제로 달라 보이는가" 는 그림 없이 답할 수 없는
질문이고, 그림 없이 답하면 남는 것은 이름을 보고 짐작하는 것뿐이라 — 그것이 이 이슈가
막으려는 바로 그 판정기가 된다.

그래도 캡처를 못 가져오는 것은 실패가 아니다. 주소는 서명된 단기 주소라 만료될 수 있고,
`capture_url` 이 아예 없는 제안도 정상이다. 그때는 글만 보고 판단하되 **그림이 있었으면
알았을 것은 답하지 않는다** 고 말해 준다(`prompt.NO_CAPTURE_NOTE`).
"""

import base64
import logging

from langchain_core.messages import HumanMessage

from app.agents.qa.vision import CaptureFetchError, download_capture, image_mime_of
from app.qa.envelope import ScreenSelectorProposalPayload, ScreenSelectorScreenRef

logger = logging.getLogger(__name__)


async def fetch_proposal_captures(
    proposal: ScreenSelectorProposalPayload,
) -> list[HumanMessage]:
    """제안이 싣고 온 캡처들, 오래된 것부터. 하나도 못 가져오면 빈 목록.

    둘뿐이고 순서가 있다. 이 판정이 묻는 것이 "두 화면이 달라 보이는가" 라, 앞뒤로 놓고
    보는 것이 질문의 전부다 — 순서를 섞으면 모델이 무엇에서 무엇으로 갔는지를 거꾸로
    읽는다.
    """
    messages: list[HumanMessage] = []
    for screen, label in (
        (proposal.previous_screen, "the previous screen"),
        (proposal.current_screen, "the current screen"),
    ):
        message = await _capture_message(screen, label)
        if message is not None:
            messages.append(message)
    return messages


async def _capture_message(
    screen: ScreenSelectorScreenRef | None, label: str
) -> HumanMessage | None:
    if screen is None or not screen.capture_url:
        return None
    try:
        raw = await download_capture(screen.capture_url)
        mime_type = image_mime_of(raw)
    except CaptureFetchError as error:
        # 판정을 세우지 않는다. 캡처 하나가 안 와서 판정이 아예 안 나가면 그 후보들은
        # 저쪽 장부에 "물어봤다" 로 남은 채 영영 답을 못 받는다.
        logger.warning(
            "[screen-verdict] capture for %s could not be fetched: %s", label, error
        )
        return None

    encoded = base64.b64encode(raw).decode("ascii")
    caption = f"This is {label} (screen {screen.screen_id})."
    return HumanMessage(
        content=[
            {"type": "text", "text": caption},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
            },
        ]
    )
