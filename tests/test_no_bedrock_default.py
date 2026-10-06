"""A default install never needs Bedrock: the router default, the catalog list and boot."""

import pytest
from fastapi.testclient import TestClient

from app.agents.scenario.router import router_model
from app.config import Settings, get_settings
from app.llm.models import DEFAULT_MODEL, LLMModel, required_openrouter_slugs

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
    assert router_model() is DEFAULT_MODEL
    assert not router_model().value.startswith("bedrock/")


def test_router_model_can_be_set_to_bedrock_by_an_operator(no_bedrock, monkeypatch):
    monkeypatch.setenv("ROUTER_MODEL", LLMModel.claude_haiku_4_5_bedrock.value)
    get_settings.cache_clear()

    assert router_model() is LLMModel.claude_haiku_4_5_bedrock


def test_router_model_outside_the_catalog_is_rejected(no_bedrock):
    with pytest.raises(ValueError, match="not in the catalog"):
        Settings(_env_file=None, router_model="nope/none")


def test_default_router_builds_without_any_bedrock_value(no_bedrock):
    from app.llm.chat_model import build_chat_model
    from app.llm.models import ReasoningConfig

    build_chat_model.cache_clear()
    chat = build_chat_model(router_model(), ReasoningConfig(max_tokens=0))

    assert type(chat).__name__ == "ChatOpenAI"


def test_required_slugs_cover_catalog_and_embeddings_without_bedrock():
    slugs = required_openrouter_slugs("openai/text-embedding-3-large")

    assert len(slugs) == 13
    assert "openai/text-embedding-3-large" in slugs
    assert DEFAULT_MODEL.value in slugs
    assert not any(slug.startswith("bedrock/") for slug in slugs)
    assert len(slugs) == len(set(slugs))


def test_required_models_route_and_app_boot_without_bedrock(no_bedrock):
    from app.main import create_app

    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/internal/models/required")

    assert response.status_code == 200
    assert "openai/text-embedding-3-large" in response.json()["slugs"]
