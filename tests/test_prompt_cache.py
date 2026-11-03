"""PE 엔진의 Claude 호출이 고정 앞부분 끝에 cache 경계를 두는지(`app.llm.chat_model.prompt_messages`).

런 154~160 의 PE Claude 호출은 cache 읽기·쓰기가 모두 0 이었다. 끝에 경계 하나를 두는 top-level `cache_control` 은 끝이
매번 다른 단발 호출에서 읽히지 않는다.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.llm.chat_model import MIN_CACHED_PREFIX_CHARS, prompt_messages

SYSTEM = "rules " * 2_000
STABLE = "Developer macros:\n" + "- macro\n" * 1_000


def test_claude_marks_the_system_and_the_stable_head_of_the_request() -> None:
    system, user = prompt_messages("claude/claude-sonnet-5-5", SYSTEM, STABLE + "Write step 3 now: ...", STABLE)

    assert isinstance(system, SystemMessage) and system.content[0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(user, HumanMessage)
    assert user.content[0] == {"type": "text", "text": STABLE, "cache_control": {"type": "ephemeral"}}
    assert user.content[1] == {"type": "text", "text": "Write step 3 now: ..."}


def test_short_parts_and_a_request_that_does_not_start_with_the_head_are_left_plain() -> None:
    short = "x" * (MIN_CACHED_PREFIX_CHARS - 1)
    system, user = prompt_messages("claude/claude-sonnet-5-5", short, "Write step 1", STABLE)

    assert system.content == short
    assert user.content == "Write step 1"


def test_other_providers_get_plain_messages() -> None:
    system, user = prompt_messages("openai/gpt-6-luna", SYSTEM, STABLE + "rest", STABLE)

    assert system.content == SYSTEM and user.content == STABLE + "rest"
