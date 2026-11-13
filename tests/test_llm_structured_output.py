"""구조화 출력 방식이 model capability 와 provider client 에 맞게 갈린다."""

from unittest.mock import MagicMock

import pytest

from app.llm.chat_model import select_structured_method, structured


class _Schema:
    """`with_structured_output` 에 넘어갈 자리만 차지한다. 값은 안 쓴다."""


def _capture(monkeypatch) -> MagicMock:
    """`build_chat_model` 을 가로채 `with_structured_output` 이 무엇을 받았는지 본다."""
    chat = MagicMock()
    monkeypatch.setattr("app.llm.chat_model.build_chat_model", lambda model: chat)
    return chat


OPENROUTER = "openai/gpt-6-luna"


def test_openrouter_still_gets_strict(monkeypatch) -> None:
    """OpenRouter 경로는 strict json_schema 다."""
    chat = _capture(monkeypatch)

    structured(OPENROUTER, _Schema)

    chat.with_structured_output.assert_called_once_with(
        _Schema, method="json_schema", strict=True
    )


def test_a_reasoning_setting_reaches_the_chat_model(monkeypatch) -> None:
    """결정 질문은 추론을 끈 model 로 묻는다(`app/llm/decision.py`)."""
    from app.llm.models import ReasoningConfig

    built: list[tuple] = []
    chat = MagicMock()
    monkeypatch.setattr(
        "app.llm.chat_model.build_chat_model",
        lambda model, reasoning=None: built.append((model, reasoning)) or chat,
    )
    off = ReasoningConfig(max_tokens=0)

    structured(OPENROUTER, _Schema, off)

    assert built == [(OPENROUTER, off)]


@pytest.mark.parametrize("model", [OPENROUTER, "claude/claude-sonnet-5-5"])
def test_no_call_site_decides_this_for_itself(model) -> None:
    """네 agent 가 각자 고르지 않고 이 함수를 지난다.

    종전에는 넷이 같은 분기를 복사해 갖고 있었고, 새 provider 가 들어오자 넷이 함께 죽었다.
    한 곳이 알면 다음 provider 가 늘어도 고칠 자리가 하나다.
    """
    import inspect

    from app.agents.game_context import agent as game_context
    from app.agents.knowledge_query import agent as knowledge_query
    from app.agents.screen_verdict import agent as screen_verdict
    from app.agents.step_phrasing import agent as step_phrasing

    for module in (game_context, knowledge_query, screen_verdict, step_phrasing):
        source = inspect.getsource(module)
        assert "with_structured_output" not in source, module.__name__
        assert "strict=True" not in source, module.__name__
