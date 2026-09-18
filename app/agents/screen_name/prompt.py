"""이름 짓는 agent 가 모델에게 보내는 것.

`screen` 과 `scene` 을 JSON 으로 그대로 싣는다. `screen_verdict/prompt.py` 와 같은 이유다 —
문장으로 풀어 쓰는 것이 곧 요약이고, 요약하는 사람이 무엇이 중요한지를 이미 정해 버린다.

`capture_url` 과 `capture_expires_at` 은 빼고 싣는다. 서명된 단기 주소는 화면에 대해
아무것도 말하지 않는 긴 문자열이고, 그림 자체는 자기 turn 으로 따로 온다. 남겨 두면 모델이
읽을 것이 하나 더 생기고 그중 어느 글자도 근거가 아니다.

screen capture 는 human 메시지 뒤에 붙는다. 이미지 블록은 tool 결과에도 system 메시지에도
실을 수 없다(`app/agents/qa/vision.py`).
"""

import json

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.agents.screen_name.schemas import ScreenNameRequest
from app.prompts import load_prompt
from app.qa.envelope import MAX_SCREEN_NAME_LENGTH

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "screen_name"

SYSTEM_ROLE = "system"
HUMAN_ROLE = "human"

# screen capture 를 뒤에 붙일 자리.
CAPTURE_SLOT = "captures"

# 주소는 근거가 아니라 전송 수단이다. 이 둘만 빼고 `screen` 을 그대로 싣는다.
_TRANSPORT_FIELDS = ("capture_url", "capture_expires_at")

OUTPUT_CONTRACT = {
    "name": (
        "a short noun phrase naming this screen, or null if the evidence does not "
        "support one"
    ),
    "note": "one sentence about the naming, or null",
}

# screen capture 가 없을 때 human 메시지에 들어가는 말. 빈 문자열로 두지 않는 이유는 라벨만
# 남은 자리가 모델에게 "그림이 있었는데 못 봤다" 로 읽히고, 그때 모델은 본 적 없는 화면을
# 묘사하기 시작하기 때문이다.
NO_CAPTURE_NOTE = (
    "No capture came with this request. Name the screen from the discriminator alone "
    "if those selectors name something a person would recognise as a screen, and "
    "answer null rather than guessing at anything you would have needed a picture to "
    "know."
)

CAPTURE_NOTE = "The capture of this screen follows this message."


def build_screen_name_prompt(version: str | None = None) -> ChatPromptTemplate:
    system = load_prompt(PROMPT_AGENT, SYSTEM_ROLE, version)
    human = load_prompt(PROMPT_AGENT, HUMAN_ROLE, system.version)
    return ChatPromptTemplate.from_messages(
        [
            ("system", system.body),
            ("human", human.body),
            MessagesPlaceholder(CAPTURE_SLOT),
        ]
    )


def build_chain_inputs(request: ScreenNameRequest, captures: list) -> dict:
    """모델에게 갈 값들. `captures` 는 `HumanMessage` 목록이고 비어도 된다."""
    screen = request.screen.model_dump(mode="json")
    for field in _TRANSPORT_FIELDS:
        screen.pop(field, None)
    return {
        "max_name_length": str(MAX_SCREEN_NAME_LENGTH),
        # `exclude_none` 을 쓰지 않는다. `name: null` 은 "지도가 아직 이 화면을 뭐라고
        # 부르지 않는다" 는 사실이고, 키를 지우면 그 사실이 사라진다.
        "screen": json.dumps(screen, ensure_ascii=False, indent=2),
        "scene": json.dumps(
            request.scene.model_dump(mode="json"), ensure_ascii=False, indent=2
        ),
        "capture_note": CAPTURE_NOTE if captures else NO_CAPTURE_NOTE,
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False, indent=2),
        CAPTURE_SLOT: captures,
    }
