"""The planner's call takes the route its model id names, with the key the tool loop would use.

Claude Opus 5.5 and Sonnet 5.5 go to the Claude API on the platform key, or OpenRouter under
their OpenRouter ids; GPT-6.1 Sol goes to the OpenAI API on the user's key, or OpenRouter. No call
reaches the network: the built client's `ainvoke` is replaced.
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.macros import RegisteredMacros
from app.agents.planner_executer.planner import Planner
from app.agents.qa.arch import QaArchSpec
from app.llm.chat_model import OPENAI_API_BASE_URL, build_chat_model
from app.llm.claude_api import ClaudeApiChat
from app.llm.credentials import LlmKey, LlmKeyService, bind_llm_key
from app.qa.run_config import resolve_run_config
from app.qa.schemas import QaScenario, QaStep
from tests.planner_executer_fakes import FakeGame
from tests.test_planner_executer_steps import MACROS


class _PlatformKeys:
    """Stands in for `LlmApiKeyProvider`: the platform keys an administrator stored."""

    def get_anthropic_key(self) -> str | None:
        return "sk-ant-platform"

    def get_bedrock_key(self) -> str | None:
        return None

    def get_openrouter_key_or_placeholder(self) -> str:
        return "sk-or-platform"

    def invalidate(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr("app.llm.chat_model.get_llm_api_key_provider", lambda: _PlatformKeys())
    build_chat_model.cache_clear()
    bind_llm_key(None)
    yield
    bind_llm_key(None)
    build_chat_model.cache_clear()


def _write(model: str, monkeypatch) -> tuple[object, list]:
    """Write one step with `model`; return the client the planner built and what it was sent."""
    sent: list = []

    async def answer(self, messages, *args, **kwargs):
        sent.append((type(self), messages))
        return AIMessage(content=[{"type": "text", "text": f"```python\n{MACROS}```"}])

    built = build_chat_model(model)
    monkeypatch.setattr(type(built), "ainvoke", answer)
    config = resolve_run_config(
        model=model, arch=QaArchSpec(engine="planner_executer", planner=PlannerExecuterArchSpec())
    )
    fake = FakeGame("Menu", {"Root/Start": {"text": "Start"}})

    async def go():
        fake.push_reading()
        planner = Planner(config, fake.channel, config.arch.planner, RegisteredMacros(fake.channel))
        return await planner.write(QaScenario(title="S", steps=[QaStep(action="Press Start.")]), 1)

    written = asyncio.run(go())
    assert written.refusal is None and "press_start" in written.macros.actions
    return built, sent


@pytest.mark.parametrize("model", ["claude/claude-opus-5-5", "claude/claude-sonnet-5-5"])
def test_claude_models_go_to_the_claude_api(model: str, monkeypatch) -> None:
    built, sent = _write(model, monkeypatch)

    assert isinstance(built, ClaudeApiChat)
    assert built.anthropic_api_key.get_secret_value() == "sk-ant-platform"
    assert len(sent) == 1


def test_claude_on_the_users_key_uses_that_key(monkeypatch) -> None:
    bind_llm_key(LlmKey(service=LlmKeyService.anthropic, api_key="sk-ant-user"))

    built, _ = _write("claude/claude-sonnet-5-5", monkeypatch)

    assert built.anthropic_api_key.get_secret_value() == "sk-ant-user"


def test_gpt_6_1_sol_goes_to_the_openai_api_on_the_users_key(monkeypatch) -> None:
    bind_llm_key(LlmKey(service=LlmKeyService.openai, api_key="sk-user-openai"))

    built, sent = _write("gpt/gpt-6.1-sol", monkeypatch)

    assert isinstance(built, ChatOpenAI) and built.openai_api_base == OPENAI_API_BASE_URL
    assert len(sent) == 1


@pytest.mark.parametrize(
    "model", ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "openai/gpt-6.1-sol"]
)
def test_openrouter_ids_go_to_openrouter(model: str, monkeypatch) -> None:
    built, sent = _write(model, monkeypatch)

    assert isinstance(built, ChatOpenAI) and built.openai_api_base != OPENAI_API_BASE_URL
    assert len(sent) == 1
