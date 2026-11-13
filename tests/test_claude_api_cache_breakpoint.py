"""Anthropic 요청의 캐시 경계. 시스템 프롬프트 끝, 그리고 이번 호출에만 붙는 메시지 바로 앞."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from app.llm.cache_boundary import PER_CALL_KEY, per_call
from app.llm.chat_model import _OpenRouterAnthropicChat
from app.llm.claude_api import (
    ClaudeApiChat,
    _with_stored_breakpoint,
    _with_system_breakpoint,
)


def test_문자열_시스템_프롬프트는_경계를_단_블록이_된다() -> None:
    out = _with_system_breakpoint([SystemMessage(content="rules"), HumanMessage(content="hi")])

    assert out[0].content == [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral"}}]
    assert out[1].content == "hi"


def test_블록_시스템_프롬프트는_마지막_블록에만_경계를_단다() -> None:
    system = SystemMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])

    out = _with_system_breakpoint([system])

    assert out[0].content == [{"type": "text", "text": "a"}, {"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}]


def test_시스템_프롬프트가_없으면_그대로다() -> None:
    messages = [HumanMessage(content="hi")]

    assert _with_system_breakpoint(messages) is messages


# --- 이번 호출에만 붙는 메시지 앞의 경계 ----------------------------------------------

CACHED = {"type": "ephemeral"}


def _stored() -> list:
    return [
        SystemMessage(content="rules"),
        HumanMessage(content="start"),
        AIMessage(content="", tool_calls=[{"name": "observe_scene", "args": {}, "id": "call_1"}]),
        ToolMessage(content="scene view", tool_call_id="call_1"),
    ]


def test_per_call_메시지_바로_앞_저장된_메시지에_경계를_단다() -> None:
    out = _with_stored_breakpoint([*_stored(), per_call(HumanMessage(content="latest pulse"))])

    assert out[3].content == [{"type": "text", "text": "scene view", "cache_control": CACHED}]
    assert out[4].content == "latest pulse"
    assert out[:3] == _stored()[:3]


def test_per_call_메시지가_없으면_그대로다() -> None:
    messages = _stored()

    assert _with_stored_breakpoint(messages) is messages


def test_per_call_앞이_답_메시지면_경계를_달지_않는다() -> None:
    messages = [*_stored()[:3], per_call(HumanMessage(content="latest pulse"))]

    assert _with_stored_breakpoint(messages) is messages


def test_경계는_tool_result_로_올라가고_표시는_요청에_실리지_않는다() -> None:
    model = ClaudeApiChat(model="claude-sonnet-5-5", api_key="sk-ant-test")
    messages = _with_stored_breakpoint([*_stored(), per_call(HumanMessage(content="latest pulse"))])

    payload = model._get_request_payload(messages)

    last = payload["messages"][-1]
    assert last["role"] == "user"
    assert last["content"][0]["type"] == "tool_result"
    assert last["content"][0]["cache_control"] == CACHED
    assert last["content"][-1] == {"type": "text", "text": "latest pulse"}
    assert PER_CALL_KEY not in json.dumps(payload)


@pytest.mark.parametrize("use_responses_api", [False, True])
def test_OpenAI_요청에도_표시는_실리지_않는다(use_responses_api) -> None:
    model = ChatOpenAI(model="gpt-6-luna", api_key="sk-test", use_responses_api=use_responses_api)

    payload = model._get_request_payload([HumanMessage(content="start"), per_call(HumanMessage(content="latest pulse"))])

    assert PER_CALL_KEY not in json.dumps(payload)


# --- OpenRouter 로 부르는 Anthropic 모델 ------------------------------------------


def _openrouter() -> _OpenRouterAnthropicChat:
    return _OpenRouterAnthropicChat(model="anthropic/claude-sonnet-5.5", api_key="sk-or-test", base_url="https://openrouter.ai/api/v1")


def test_OpenRouter_Anthropic_은_요청_본문의_tool_메시지에_경계를_단다() -> None:
    payload = _openrouter()._get_request_payload([*_stored(), per_call(HumanMessage(content="latest pulse"))])

    tool, summary = payload["messages"][-2:]
    assert tool["role"] == "tool"
    assert tool["content"] == [{"type": "text", "text": "scene view", "cache_control": CACHED}]
    assert summary == {"role": "user", "content": "latest pulse"}
    assert PER_CALL_KEY not in json.dumps(payload)


def test_OpenRouter_Anthropic_은_per_call_메시지가_없으면_본문을_바꾸지_않는다() -> None:
    plain = ChatOpenAI(model="anthropic/claude-sonnet-5.5", api_key="sk-or-test", base_url="https://openrouter.ai/api/v1")

    assert _openrouter()._get_request_payload(_stored()) == plain._get_request_payload(_stored())


def test_OpenRouter_Anthropic_은_답_메시지에는_경계를_달지_않는다() -> None:
    messages = [*_stored()[:3], per_call(HumanMessage(content="latest pulse"))]

    payload = _openrouter()._get_request_payload(messages)

    assert "cache_control" not in json.dumps(payload)
