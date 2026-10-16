"""한 메시지에 요청이 여럿이면 첫 요청만 처리한다 (ARTEL-1086).

"21번 스텝을 빼 줘. 아 그리고 지금 TC 는 몇 개야?" 처럼 고치기와 질문이 한 말에 섞이면, 한 갈래가 둘을 함께 받아
고치기도 대충 하고 질문도 엉뚱한 범위로 답했다(런 104). 요청마다 맞는 갈래가 다르므로 **처음 말한 요청만** 그 갈래로
보내고, 나머지는 따로 다시 말해 달라고 안내한다.

모든 말에 모델을 부르지 않는다. 섞였을 법한 말(이어 주는 말 · 물음 여럿)만 골라 그때만 나눈다.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from app.llm.chat_model import structured
from app.llm.models import LLMModel, ReasoningConfig

logger = logging.getLogger(__name__)

# 요청을 이어 붙이는 말. 이것도 물음표도 없으면 한 요청으로 본다.
_JOINERS = re.compile(r"(그리고|아 그리고|그리고요|또한|그 다음에|하고 나서|그러고 나서|\band also\b|\balso\b)", re.IGNORECASE)


class SplitRequests(BaseModel):
    first: str = Field(description="처음 말한 요청. 사용자의 말을 될 수 있는 대로 그대로 옮긴다.")
    rest: list[str] = Field(default_factory=list, description="그 뒤의 다른 요청들. 하나뿐이면 빈 목록.")


_PROMPT = """사용자 메시지를 서로 다른 요청으로 나눈다.

- 요청 = 따로 처리해야 하는 일 하나(시나리오 만들기, 시나리오 고치기, 정보 묻기 등).
- 한 요청 안의 여러 조건 · 세부 사항은 나누지 않는다. 예: "보스전 시나리오 만들고 보상 확인까지 넣어 줘" 는 요청 하나다.
- first 에는 처음 말한 요청을, rest 에는 그 뒤의 요청을 사용자의 말 그대로 옮긴다. 요청이 하나면 rest 는 비운다.

메시지:
{message}"""


def looks_multiple(text: str) -> bool:
    """여러 요청이 섞였을 법한가. 거짓이면 나누지 않는다(모델을 부르지 않는다)."""
    return bool(_JOINERS.search(text)) or text.count("?") + text.count("？") >= 2


async def split_requests(model: LLMModel, text: str) -> SplitRequests | None:
    """요청을 나눈다. 실패하면 None — 나누지 못했다고 턴을 막지 않는다(그때는 예전처럼 통째로 간다)."""
    try:
        answer = await structured(model, SplitRequests, ReasoningConfig(max_tokens=0)).ainvoke(
            _PROMPT.format(message=text)
        )
    except Exception:  # noqa: BLE001 - 나누기는 곁가지다
        logger.warning("[scenario] request split failed; handling the message whole", exc_info=True)
        return None
    if not isinstance(answer, SplitRequests) or not answer.first.strip():
        return None
    return answer


def rest_note(rest: list[str], locale: str) -> str:
    """처리하지 않은 요청을 알린다."""
    joined = " / ".join(f"“{one.strip()}”" for one in rest if one.strip())
    if locale == "ko":
        return f"한 번에 한 가지씩 처리해요. 이번에는 첫 요청만 처리했어요 — {joined} 은(는) 이어서 다시 말씀해 주세요."
    return f"I handle one request at a time and only did the first one. Please send {joined} again separately."
