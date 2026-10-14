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


BEDROCK = "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_WITHOUT_STRICT = "bedrock/us.anthropic.claude-haiku-5-5"
OPENROUTER = "openai/gpt-6-luna"


def test_bedrock_gets_json_schema_without_strict(monkeypatch) -> None:
    """`ChatBedrockConverse._converse_params()` 가 `strict` 인자를 받지 않는다
    (ARTEL-806). `json_schema` 자체는 Bedrock 도 받으므로 `json_mode` 로 내리지 않는다."""
    chat = _capture(monkeypatch)

    structured(BEDROCK, _Schema)

    chat.with_structured_output.assert_called_once_with(_Schema, method="json_schema")
    assert "strict" not in chat.with_structured_output.call_args.kwargs


def test_bedrock_without_structured_outputs_uses_function_calling(monkeypatch) -> None:
    """Haiku 5.5 Bedrock 은 structured outputs 미지원이라 tool schema 로 받는다."""
    chat = _capture(monkeypatch)

    structured(BEDROCK_WITHOUT_STRICT, _Schema)

    chat.with_structured_output.assert_called_once_with(
        _Schema, method="function_calling"
    )


def test_openrouter_still_gets_strict(monkeypatch) -> None:
    """OpenRouter 경로는 그대로다. 이 결함은 Bedrock 에서만 났다."""
    chat = _capture(monkeypatch)

    structured(OPENROUTER, _Schema)

    chat.with_structured_output.assert_called_once_with(
        _Schema, method="json_schema", strict=True
    )


def test_bedrock_catalog_reports_strict_schema_support_per_row() -> None:
    assert select_structured_method(BEDROCK) == "json_schema"
    assert select_structured_method(BEDROCK_WITHOUT_STRICT) == "json_mode"


@pytest.mark.parametrize("model", [BEDROCK, BEDROCK_WITHOUT_STRICT, OPENROUTER])
def test_no_call_site_decides_this_for_itself(model) -> None:
    """네 agent 가 각자 고르지 않고 이 함수를 지난다.

    종전에는 넷이 같은 분기를 복사해 갖고 있었고, Bedrock 이 들어오자 넷이 함께 죽었다.
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
