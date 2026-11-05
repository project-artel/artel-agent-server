import os

import pytest

from app.config import Settings
from app.observability import configure_langsmith

LANGSMITH_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_PROJECT",
)


@pytest.fixture(autouse=True)
def isolate_langsmith_env(monkeypatch) -> None:
    # Keeps the developer's real keys out of Settings, and lets monkeypatch
    # restore whatever configure_langsmith writes into os.environ.
    for name in LANGSMITH_VARS:
        monkeypatch.delenv(name, raising=False)


def test_tracing_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert configure_langsmith(settings) is False


def test_tracing_exports_env_vars() -> None:
    settings = Settings(
        _env_file=None,
        APP_ENV="stage",
        langsmith_tracing=True,
        langsmith_api_key="ls-test-key",
    )

    assert configure_langsmith(settings) is True
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert os.environ["LANGSMITH_API_KEY"] == "ls-test-key"
    assert os.environ["LANGSMITH_ENDPOINT"] == "https://api.smith.langchain.com"
    assert os.environ["LANGSMITH_PROJECT"] == "artel-agent-server-stage"


def test_explicit_project_overrides_default() -> None:
    settings = Settings(
        _env_file=None,
        langsmith_tracing=True,
        langsmith_api_key="ls-test-key",
        langsmith_project="qa-experiments",
    )

    configure_langsmith(settings)

    assert os.environ["LANGSMITH_PROJECT"] == "qa-experiments"


def test_tracing_stays_off_without_api_key() -> None:
    settings = Settings(_env_file=None, langsmith_tracing=True)

    assert configure_langsmith(settings) is False
    assert os.environ["LANGSMITH_TRACING"] == "false"
