"""판정 agent 가 모델에게 보내는 것.

제안 payload 를 JSON 으로 그대로 싣는다. 문장으로 풀어 쓰지 않는 이유는 그 풀어 쓰기가
곧 요약이고, 요약하는 사람이 무엇이 중요한지를 이미 정해 버리기 때문이다 — 이 판정은
게임을 모른 채 나야 하므로 무엇이 중요한지를 정하는 것도 모델의 몫이다.

캡처는 자기 턴으로 따로 온다. 이미지 블록은 도구 결과에도 시스템 메시지에도 실을 수 없고
(`app/agents/qa/vision.py`), 여기서는 human 메시지 뒤에 붙는다.
"""

import json

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.agents.qa.screen import MAX_PATTERN_LENGTH, SCREEN_SELECTOR_MATCHES
from app.agents.screen_verdict.schemas import ScreenVerdictRequest
from app.prompts import load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "screen_verdict"

SYSTEM_ROLE = "system"
HUMAN_ROLE = "human"

# 캡처를 뒤에 붙일 자리. 없으면 빈 리스트가 들어가고 프롬프트는 글자 하나 달라지지 않는다.
CAPTURE_SLOT = "captures"

OUTPUT_CONTRACT = {
    "entries": [
        {
            "match": " | ".join(SCREEN_SELECTOR_MATCHES),
            "pattern": "an exact string copied from the proposal, never a regular expression",
            "screen_defining": "true if it tells screens apart in this scene, false if it is ignored",
            "reason": "one sentence saying what you saw, for someone who was not here",
        }
    ],
    "note": "one sentence about the proposal as a whole, or null",
}

# 캡처가 없을 때 human 메시지에 들어가는 말. 빈 문자열로 두지 않는 이유는 라벨만 남은
# 자리가 모델에게 "그림이 있었는데 못 봤다" 로 읽히고, 그때 모델은 본 적 없는 화면을
# 묘사하기 시작하기 때문이다.
NO_CAPTURE_NOTE = (
    "No capture came with this proposal. Judge from the discriminators, the changes, "
    "and the candidate statistics alone, and leave out anything you would have needed "
    "a picture to be sure of."
)

CAPTURE_NOTE = (
    "The captures follow this message, oldest first. Each one says which screen it is."
)


def build_screen_verdict_prompt(version: str | None = None) -> ChatPromptTemplate:
    system = load_prompt(PROMPT_AGENT, SYSTEM_ROLE, version)
    human = load_prompt(PROMPT_AGENT, HUMAN_ROLE, system.version)
    return ChatPromptTemplate.from_messages(
        [
            ("system", system.body),
            ("human", human.body),
            MessagesPlaceholder(CAPTURE_SLOT),
        ]
    )


def build_chain_inputs(request: ScreenVerdictRequest, captures: list) -> dict:
    """모델에게 갈 값들. `captures` 는 `HumanMessage` 목록이고 비어도 된다."""
    return {
        "max_pattern_length": str(MAX_PATTERN_LENGTH),
        # `exclude_none` 을 쓰지 않는다. `previous_screen: null` 은 "런의 첫 화면이거나
        # 저쪽이 재시작했다" 는 사실이고, 키를 지우면 그 사실이 사라진다.
        "proposal": json.dumps(
            request.proposal.model_dump(mode="json"), ensure_ascii=False, indent=2
        ),
        "capture_note": CAPTURE_NOTE if captures else NO_CAPTURE_NOTE,
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False, indent=2),
        CAPTURE_SLOT: captures,
    }
