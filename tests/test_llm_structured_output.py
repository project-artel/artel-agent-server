"""구조화 출력이 provider 마다 다른 인자를 받는다.

`supports_strict_json` 은 **모델이 엄격한 스키마를 아는가**를 적어 둔 칸이고, 그것과
**클라이언트가 `strict` 인자를 받는가**는 다른 질문이다. Bedrock 에서 그 둘이 갈라진다
(ARTEL-806).
"""

from unittest.mock import MagicMock

import pytest

from app.llm.chat_model import select_structured_method, structured
from app.llm.models import LLMModel


class _Schema:
    """`with_structured_output` 에 넘어갈 자리만 차지한다. 값은 안 쓴다."""


def _capture(monkeypatch) -> MagicMock:
    """`build_chat_model` 을 가로채 `with_structured_output` 이 무엇을 받았는지 본다."""
    chat = MagicMock()
    monkeypatch.setattr("app.llm.chat_model.build_chat_model", lambda model: chat)
    return chat


BEDROCK = LLMModel.claude_haiku_4_5_bedrock
OPENROUTER = LLMModel.gpt_5_6_luna


def test_bedrock_gets_json_schema_without_strict(monkeypatch) -> None:
    """**`strict` 를 빼는 것이 이 티켓의 전부다.**

    `ChatBedrockConverse._converse_params()` 가 그 인자를 안 받아
    `got an unexpected keyword argument 'strict'` 로 죽었다. 예외를 삼키는 호출부라
    런은 정상 종료로 보였고 화면 판정만 조용히 사라졌다.

    `json_schema` 자체는 Bedrock 도 받는다. 그래서 `json_mode` 로 물러서지 않는다 —
    물러서면 스키마를 아는 모델에게 덜 엄격한 요청을 보내게 된다.
    """
    chat = _capture(monkeypatch)

    structured(BEDROCK, _Schema)

    chat.with_structured_output.assert_called_once_with(_Schema, method="json_schema")
    assert "strict" not in chat.with_structured_output.call_args.kwargs


def test_openrouter_still_gets_strict(monkeypatch) -> None:
    """OpenRouter 경로는 그대로다. 이 결함은 Bedrock 에서만 났다."""
    chat = _capture(monkeypatch)

    structured(OPENROUTER, _Schema)

    chat.with_structured_output.assert_called_once_with(
        _Schema, method="json_schema", strict=True
    )


def test_bedrock_still_declares_that_it_knows_strict_schemas() -> None:
    """`supports_strict_json` 을 거짓으로 바꿔 때우지 않았다.

    그 칸의 뜻은 "모델이 엄격한 스키마를 안다" 이고 그것은 참이다. 거짓으로 적으면
    다음 사람이 그 값을 믿고 다른 판단을 한다 — 갈라야 하는 것은 모델의 능력이 아니라
    클라이언트가 받는 인자다.
    """
    assert select_structured_method(BEDROCK) == "json_schema"


@pytest.mark.parametrize("model", [BEDROCK, OPENROUTER])
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
