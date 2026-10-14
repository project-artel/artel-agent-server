"""A default install never needs Bedrock: the router default and boot."""

import pytest
from fastapi.testclient import TestClient

from app.agents.scenario.router import router_model
from app.config import Settings, get_settings
from app.llm.models import DEFAULT_MODEL

BEDROCK_ENVIRONMENT = [
    "ROUTER_MODEL",
    "BEDROCK_REGION",
    "BEDROCK_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_PROFILE",
]


@pytest.fixture
def no_bedrock(monkeypatch):
    for name in BEDROCK_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_router_defaults_to_an_openrouter_model(no_bedrock):
    assert Settings(_env_file=None).router_model == ""
    assert router_model() == DEFAULT_MODEL
    assert not router_model().startswith("bedrock/")


def test_router_model_can_be_set_to_bedrock_by_an_operator(no_bedrock, monkeypatch):
    monkeypatch.setenv("ROUTER_MODEL", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0")
    get_settings.cache_clear()

    assert router_model() == "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"


def test_router_model_outside_the_catalog_is_rejected_when_used(no_bedrock, monkeypatch):
    """Settings load before the catalog is reachable, so the check runs at use."""
    monkeypatch.setenv("ROUTER_MODEL", "nope/none")
    get_settings.cache_clear()

    with pytest.raises(ValueError, match="router_model .*not in the catalog"):
        router_model()


def test_router_model_may_name_a_disabled_row(no_bedrock, monkeypatch):
    """Turning a model off hides it from users; an operator's router keeps it."""
    monkeypatch.setenv("ROUTER_MODEL", "bedrock/us.openai.gpt-6.1-sol")
    get_settings.cache_clear()

    assert router_model() == "bedrock/us.openai.gpt-6.1-sol"


def test_default_router_builds_without_any_bedrock_value(no_bedrock):
    from app.llm.chat_model import build_chat_model
    from app.llm.models import ReasoningConfig

    build_chat_model.cache_clear()
    chat = build_chat_model(router_model(), ReasoningConfig(max_tokens=0))

    assert type(chat).__name__ == "ChatOpenAI"


def test_app_boots_without_bedrock_and_the_model_routes_are_gone(no_bedrock):
    from app.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/internal/models").status_code == 404
        assert client.get("/internal/models/required").status_code == 404
