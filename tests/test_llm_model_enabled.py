"""Where a disabled model is refused, where it still works, and the Bedrock request fields."""

import pytest
from fastapi.testclient import TestClient
from langchain_aws import ChatBedrockConverse
from pydantic import ValidationError

import app.llm.catalog as catalog_module
from app.api.qa_sessions import OpenQaSessionRequest
from app.api.sessions import OpenSessionRequest, TurnMessage
from app.llm.catalog import ModelCatalogUnavailableError
from app.llm.chat_model import _CachingChatBedrockConverse, build_chat_model
from app.llm.models import ReasoningConfig, ReasoningEffort, get_model_spec
from app.llm.usage import _estimate_cost
from app.main import app
from app.qa.run_config import resolve_run_config
from tests.conftest import install_catalog_with_enabled
from tests.test_qa_prompt_version import open_request

DISABLED_HAIKU = "bedrock/us.anthropic.claude-haiku-5-5"
DISABLED_SOL = "bedrock/us.openai.gpt-6.1-sol"


@pytest.fixture(autouse=True)
def _fresh_chat_models():
    build_chat_model.cache_clear()
    yield
    build_chat_model.cache_clear()


# --- refused where a user picks the model --------------------------------------


@pytest.mark.parametrize("model", [DISABLED_HAIKU, DISABLED_SOL])
def test_qa_session_start_refuses_a_disabled_model(model: str) -> None:
    with pytest.raises(ValidationError, match="disabled by an administrator"):
        OpenQaSessionRequest.model_validate(open_request(model=model))


def test_qa_session_start_answers_422_for_a_disabled_model() -> None:
    response = TestClient(app).post(
        "/internal/qa-sessions", json=open_request(model=DISABLED_SOL)
    )

    assert response.status_code == 422
    assert "disabled by an administrator" in response.text


def test_scenario_session_create_answers_422_for_a_disabled_model() -> None:
    response = TestClient(app).post(
        "/internal/sessions", json={"user_input": "shop flow", "model": DISABLED_HAIKU}
    )

    assert response.status_code == 422
    assert "disabled by an administrator" in response.text


def test_scenario_turn_refuses_switching_to_a_disabled_model() -> None:
    with pytest.raises(ValidationError, match="disabled by an administrator"):
        TurnMessage.model_validate({"user_input": "again", "model": DISABLED_HAIKU})


def test_unknown_model_is_still_refused_on_request() -> None:
    with pytest.raises(ValidationError, match="not in the catalog"):
        OpenSessionRequest.model_validate({"user_input": "x", "model": "openai/nope"})


def test_a_model_an_administrator_turned_on_is_accepted() -> None:
    install_catalog_with_enabled(DISABLED_SOL)

    request = OpenQaSessionRequest.model_validate(
        open_request(model=DISABLED_SOL, reasoning={"effort": "xhigh"})
    )

    assert request.model == DISABLED_SOL


def test_request_answers_503_when_no_catalog_has_loaded(monkeypatch) -> None:
    class Unavailable:
        def get(self):
            raise ModelCatalogUnavailableError("The model catalog is unavailable.")

    monkeypatch.setattr(catalog_module, "_installed", Unavailable())

    response = TestClient(app, raise_server_exceptions=False).post(
        "/internal/sessions", json={"user_input": "shop flow", "model": "openai/gpt-5.6-luna"}
    )

    assert response.status_code == 503
    assert "model catalog is unavailable" in response.text


# --- internal callers resolve any row --------------------------------------------


def test_internal_callers_resolve_a_disabled_model() -> None:
    assert get_model_spec(DISABLED_HAIKU).enabled is False
    assert type(build_chat_model(DISABLED_HAIKU)) is ChatBedrockConverse
    # 꺼진 row 의 단가로도 비용을 계산한다. 지난 사용량은 계속 가격이 매겨져야 한다.
    assert (
        _estimate_cost(
            DISABLED_HAIKU,
            input_tokens=1_000_000,
            output_tokens=0,
            cached_input_tokens=0,
            cache_write_tokens=0,
        )
        == pytest.approx(0.50)
    )


def test_run_config_resolves_a_disabled_model_for_an_internal_caller() -> None:
    config = resolve_run_config(model=DISABLED_SOL)

    assert config.model == DISABLED_SOL


# --- Bedrock request fields by provider -------------------------------------------


def test_bedrock_openai_sends_reasoning_effort_and_no_thinking() -> None:
    chat = build_chat_model(DISABLED_SOL, ReasoningConfig(effort=ReasoningEffort.high))

    assert chat.additional_model_request_fields == {"reasoning_effort": "high"}
    assert chat.model_id == "us.openai.gpt-6.1-sol"
    assert chat.temperature is None
    assert chat.max_tokens == 128_000


@pytest.mark.parametrize("reasoning", [None, ReasoningConfig(max_tokens=0)])
def test_bedrock_openai_sends_nothing_when_reasoning_is_unset_or_disabled(reasoning) -> None:
    chat = build_chat_model(DISABLED_SOL, reasoning)

    assert chat.additional_model_request_fields is None


def test_bedrock_openai_never_gets_cache_points() -> None:
    chat = build_chat_model(DISABLED_SOL, cache_prompt=True)

    assert type(chat) is ChatBedrockConverse
    assert not isinstance(chat, _CachingChatBedrockConverse)


def test_bedrock_anthropic_fields_are_unchanged() -> None:
    high = build_chat_model(DISABLED_HAIKU, ReasoningConfig(effort=ReasoningEffort.high))
    off = build_chat_model(DISABLED_HAIKU, ReasoningConfig(max_tokens=0))
    cached = build_chat_model(DISABLED_HAIKU, cache_prompt=True)

    assert high.additional_model_request_fields == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    }
    assert off.additional_model_request_fields == {"thinking": {"type": "disabled"}}
    assert high.temperature is None
    assert high.max_tokens == 128_000
    assert type(cached) is _CachingChatBedrockConverse
