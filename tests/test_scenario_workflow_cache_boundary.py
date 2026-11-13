"""저작 워크플로의 단발 호출이 provider 에 맞는 cache 경계를 놓는다(ARTEL-1438).

예전에는 Bedrock 의 `cachePoint` block 을 모든 model 에 실었다. 사용자가 고른 Claude API · OpenAI model 에는 모르는
content 다. 이제 Claude API 에만 `cache_control` 경계를 놓고, 다른 provider 에는 경계 없이 보낸다.
"""

import asyncio

from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

import app.agents.scenario.workflow as workflow


class _Plan(BaseModel):
    ok: bool = True


class _Chat:
    def __init__(self, seen: list) -> None:
        self.seen = seen

    def with_structured_output(self, schema, method=None, **_):
        def answer(messages):
            self.seen.append(messages)
            return schema()

        return RunnableLambda(answer)


def _sent(monkeypatch, model: str) -> list:
    seen: list = []
    monkeypatch.setattr(workflow, "build_chat_model", lambda *_args, **_kwargs: _Chat(seen))
    prefix = "같은 앞부분 " * 1_000
    asyncio.run(workflow._call(model, None, _Plan, (prefix, "이번 묶음")))
    return seen[0]


def _blocks(message) -> list:
    return message.content if isinstance(message.content, list) else [message.content]


def test_no_model_gets_a_bedrock_cache_point(monkeypatch) -> None:
    for model in ("openai/gpt-6-luna", "claude/claude-sonnet-5-5", "gpt/gpt-6-luna"):
        messages = _sent(monkeypatch, model)
        assert not any(isinstance(block, dict) and "cachePoint" in block for m in messages for block in _blocks(m))


def test_the_claude_api_gets_a_cache_control_boundary_after_the_prefix(monkeypatch) -> None:
    system, human = _sent(monkeypatch, "claude/claude-sonnet-5-5")

    assert system.content[-1]["cache_control"] == {"type": "ephemeral"}
    assert human.content == "이번 묶음"


def test_openrouter_gets_plain_text(monkeypatch) -> None:
    system, human = _sent(monkeypatch, "openai/gpt-6-luna")

    assert isinstance(system.content, str)
    assert human.content == "이번 묶음"
