"""The project agent step: one model call, tools bound as schemas, nothing kept."""

import asyncio
from typing import Any

import httpx
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents import AgentContext
from app.agents.project_agent import (
    ProjectAgent,
    ProjectAgentMessage,
    ProjectAgentRequest,
    ProjectAgentToolCall,
)
from app.agents.project_agent.agent import flatten_content, to_langchain_messages
from app.agents.project_agent.prompt import build_system_prompt, render_page_catalog
from app.agents.project_agent.tools import TOOL_NAMES, tool_schemas
from app.api.project_agent import router


class FakeChat(BaseChatModel):
    """Answers with a canned message and remembers what it was sent."""

    reply: AIMessage
    seen: list[list[BaseMessage]] = []
    error: Exception | None = None

    @property
    def _llm_type(self) -> str:
        return "fake-project-agent"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        if self.error is not None:
            raise self.error
        self.seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=self.reply)])


class Harness:
    def __init__(self, reply: AIMessage, error: Exception | None = None) -> None:
        self.chat = FakeChat(reply=reply, seen=[], error=error)
        self.bound: list[list[dict]] = []
        self.models: list[str] = []

    def factory(self, model: str, tools_enabled: bool):
        self.models.append(model)
        if tools_enabled:
            self.bound.append(tool_schemas())
        return self.chat


CATALOG = {
    "pages": [
        {
            "id": "qa",
            "title": "QA 실행",
            "path": "qa",
            "params": ["build"],
            "description": "QA 를 시작한다.",
            "fields": [{"name": "build", "description": "대상 build"}],
            "actions": [
                {
                    "id": "start",
                    "description": "QA 시작",
                    "args": {"type": "object", "properties": {"n": {"type": "integer"}}},
                    "risk": "approve",
                }
            ],
        }
    ]
}


def _request(**overrides: Any) -> ProjectAgentRequest:
    base: dict[str, Any] = {
        "conversation_id": 12,
        "project_id": 1,
        "page_catalog": CATALOG,
        "messages": [{"role": "user", "content": "QA 돌려 줘"}],
    }
    return ProjectAgentRequest(**{**base, **overrides})


def _run(harness: Harness, request: ProjectAgentRequest):
    agent = ProjectAgent(model_factory=harness.factory)
    return asyncio.run(agent.run(request, AgentContext(session_id="t")))


def test_tool_call_response_keeps_provider_id() -> None:
    harness = Harness(
        AIMessage(
            content="QA 실행 페이지를 엽니다.",
            tool_calls=[
                {"id": "call_1", "name": "open_page", "args": {"page": "qa"}, "type": "tool_call"}
            ],
        )
    )

    result = _run(harness, _request())

    assert result.content == "QA 실행 페이지를 엽니다."
    assert [(c.id, c.name, c.args) for c in result.tool_calls] == [
        ("call_1", "open_page", {"page": "qa"})
    ]


def test_missing_tool_call_id_is_generated() -> None:
    harness = Harness(
        AIMessage(content="", tool_calls=[{"id": None, "name": "read_page", "args": {}}])
    )

    result = _run(harness, _request())

    assert result.tool_calls[0].id.startswith("call_")
    assert len(result.tool_calls[0].id) == len("call_") + 32


def test_plain_text_response_has_no_tool_calls() -> None:
    result = _run(Harness(AIMessage(content="끝났습니다.")), _request())

    assert result.content == "끝났습니다."
    assert result.tool_calls == []


def test_block_list_content_is_flattened_to_text() -> None:
    content = [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "안녕"}, "하세요"]

    assert flatten_content(content) == "안녕하세요"
    assert flatten_content(None) == ""


def test_tools_enabled_false_binds_no_tools() -> None:
    harness = Harness(AIMessage(content="text"))

    _run(harness, _request(tools_enabled=False))
    assert harness.bound == []

    _run(harness, _request(tools_enabled=True))
    assert len(harness.bound) == 1


def test_default_factory_binds_eight_tool_schemas(monkeypatch) -> None:
    from app.agents.project_agent import agent as module

    bound: dict[str, Any] = {}

    class Recorder:
        def bind_tools(self, tools):
            bound["tools"] = tools
            return "bound"

    monkeypatch.setattr(module, "build_chat_model", lambda model: Recorder())

    assert module._default_model_factory("m", True) == "bound"
    names = [t["function"]["name"] for t in bound["tools"]]
    assert names == list(TOOL_NAMES)
    assert len(names) == 8
    assert module._default_model_factory("m", False).__class__ is Recorder


def test_tool_schemas_carry_argument_shapes() -> None:
    by_name = {t["function"]["name"]: t["function"]["parameters"] for t in tool_schemas()}

    assert "qaRun" in by_name["open_page"]["properties"]["page"]["enum"]
    assert by_name["open_page"]["required"] == ["page"]
    assert by_name["ask_choice"]["properties"]["options"]["minItems"] == 2
    assert by_name["ask_choice"]["properties"]["options"]["maxItems"] == 8
    assert by_name["read_page"]["properties"] == {}


def test_messages_convert_including_a_tool_round_trip() -> None:
    request = _request(
        messages=[
            {"role": "user", "content": "QA 돌려 줘"},
            {
                "role": "assistant",
                "content": "엽니다.",
                "tool_calls": [{"id": "call_1", "name": "open_page", "args": {"page": "qa"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"status":"ok"}'},
        ]
    )
    harness = Harness(AIMessage(content="done"))

    _run(harness, request)

    system, human, ai, tool = harness.chat.seen[0]
    assert system.type == "system"
    assert isinstance(human, HumanMessage) and human.content == "QA 돌려 줘"
    assert isinstance(ai, AIMessage)
    assert ai.tool_calls[0]["id"] == "call_1"
    assert ai.tool_calls[0]["args"] == {"page": "qa"}
    assert isinstance(tool, ToolMessage)
    assert tool.tool_call_id == "call_1" and tool.content == '{"status":"ok"}'


def test_conversion_of_assistant_without_calls() -> None:
    converted = to_langchain_messages([ProjectAgentMessage(role="assistant", content="hi")])

    assert converted[0].tool_calls == []


def test_catalog_renders_as_readable_text() -> None:
    text = render_page_catalog(CATALOG["pages"])

    assert "## qa - QA 실행" in text
    assert "- path: /qa" in text
    assert "- params: build" in text
    assert "build: 대상 build" in text
    assert "start [risk: approve]: QA 시작" in text
    assert '"properties": {"n": {"type": "integer"}}' in text


def test_empty_and_sparse_catalog_do_not_fail() -> None:
    assert render_page_catalog([]) == "(no pages)"
    assert "## x" in render_page_catalog([{"id": "x"}])


def test_system_prompt_has_rules_catalog_and_language() -> None:
    ko = build_system_prompt(CATALOG["pages"], "ko")
    en = build_system_prompt(CATALOG["pages"], "en-US")

    assert "Reply in Korean." in ko and "Reply in English." not in ko
    assert "Reply in English." in en
    assert "open_page" in ko and "[event:job_finished]" in ko and "Never invent ids" in ko
    assert "start [risk: approve]" in ko


def _client(agent: ProjectAgent) -> TestClient:
    app = FastAPI()
    app.state.project_agent = agent
    app.include_router(router, prefix="/internal")
    return TestClient(app)


def _body(**overrides: Any) -> dict:
    return {
        "conversation_id": 12,
        "project_id": 1,
        "locale": "ko",
        "tools_enabled": True,
        "page_catalog": CATALOG,
        "messages": [{"role": "user", "content": "hi"}],
        **overrides,
    }


def test_endpoint_returns_content_and_tool_calls_and_books_usage() -> None:
    from app.llm import usage

    harness = Harness(
        AIMessage(content="x", tool_calls=[{"id": "c", "name": "read_page", "args": {}}])
    )
    client = _client(ProjectAgent(model_factory=harness.factory))

    response = client.post("/internal/project-agent/step", json=_body(model=None))

    assert response.status_code == 200
    assert response.json() == {
        "content": "x",
        "tool_calls": [{"id": "c", "name": "read_page", "args": {}}],
    }
    from app.llm.models import DEFAULT_MODEL

    assert harness.models == [DEFAULT_MODEL]
    assert usage.UsageScope("PROJECT_AGENT", 12).service == "PROJECT_AGENT"


def test_unknown_model_is_422() -> None:
    client = _client(ProjectAgent(model_factory=Harness(AIMessage(content="")).factory))

    response = client.post("/internal/project-agent/step", json=_body(model="nope/none"))

    assert response.status_code == 422


def test_disabled_model_is_422() -> None:
    from tests.conftest import FIXTURE_MODEL_CATALOG

    disabled = next(i for i, s in FIXTURE_MODEL_CATALOG.items() if not s.enabled)
    client = _client(ProjectAgent(model_factory=Harness(AIMessage(content="")).factory))

    response = client.post("/internal/project-agent/step", json=_body(model=disabled))

    assert response.status_code == 422


def test_provider_error_is_502_with_detail() -> None:
    error = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    client = _client(ProjectAgent(model_factory=Harness(AIMessage(content=""), error).factory))

    response = client.post("/internal/project-agent/step", json=_body())

    assert response.status_code == 502
    assert response.json()["detail"]


def test_scope_is_set_to_project_agent_with_conversation_id(monkeypatch) -> None:
    import app.api.project_agent as module

    seen: list[tuple] = []
    monkeypatch.setattr(module, "set_usage_scope", lambda *a: seen.append(a))
    client = _client(ProjectAgent(model_factory=Harness(AIMessage(content="ok")).factory))

    client.post("/internal/project-agent/step", json=_body())

    assert seen == [("PROJECT_AGENT", 12)]
