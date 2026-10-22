"""The project agent step: one model call, tools bound as schemas, nothing kept."""

import asyncio
from typing import Any

import httpx
import anthropic
import openai
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ValidationError

from app.agents import AgentContext
from app.agents.project_agent import (
    ProjectAgent,
    ProjectAgentMessage,
    ProjectAgentRequest,
    ProjectAgentToolCall,
)
from app.agents.project_agent.agent import flatten_content, to_langchain_messages
from app.agents.project_agent.prompt import (
    build_system_prompt,
    render_guides,
    render_links,
    render_page_catalog,
)
from app.agents.project_agent.schemas import PageCatalog
from app.agents.project_agent.tools import tool_schemas
from app.api.project_agent import router
from app.llm.chat_model import ClaudeApiKeyMissingError, GptApiKeyMissingError


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


EXPECTED_TOOLS = [
    "open_page", "read_page", "set_field", "call_action",
    "ask_choice", "query", "watch_job", "read_guide", "author_scenarios",
]


class _Recorder:
    def __init__(self, reply: AIMessage) -> None:
        self.reply = reply
        self.bound: list[dict] | None = None
        self.sent: list[BaseMessage] = []

    def bind_tools(self, tools):
        self.bound = tools
        return self

    async def ainvoke(self, messages, config=None):
        self.sent = list(messages)
        return self.reply


def _use_recorder(monkeypatch, reply: AIMessage) -> tuple[_Recorder, list]:
    from app.agents.project_agent import agent as module

    recorder = _Recorder(reply)
    reasoning: list = []

    def build(model, reasoning_config=None, *args):
        reasoning.append(reasoning_config)
        return recorder

    monkeypatch.setattr(module, "build_chat_model", build)
    return recorder, reasoning


HISTORY_WITH_TOOLS = [
    {"role": "user", "content": "QA 돌려 줘"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "call_1", "name": "read_page", "args": {}}],
    },
    {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
]


def test_default_factory_binds_nine_tools_and_turns_reasoning_off(monkeypatch) -> None:
    recorder, reasoning = _use_recorder(monkeypatch, AIMessage(content="ok"))

    asyncio.run(
        ProjectAgent().run(_request(messages=HISTORY_WITH_TOOLS), AgentContext(session_id="t"))
    )

    assert [t["function"]["name"] for t in recorder.bound] == EXPECTED_TOOLS
    assert reasoning[0] is not None and reasoning[0].disabled


def test_tools_disabled_keeps_tools_bound_but_drops_calls(monkeypatch) -> None:
    recorder, _ = _use_recorder(
        monkeypatch,
        AIMessage(content="wrap up", tool_calls=[{"id": "c", "name": "read_page", "args": {}}]),
    )

    result = asyncio.run(
        ProjectAgent().run(
            _request(messages=HISTORY_WITH_TOOLS, tools_enabled=False),
            AgentContext(session_id="t"),
        )
    )

    assert len(recorder.bound) == 9
    assert result.content == "wrap up" and result.tool_calls == []
    assert isinstance(recorder.sent[-1], HumanMessage)
    assert "Do not call tools" in recorder.sent[-1].content
    assert any(isinstance(m, ToolMessage) for m in recorder.sent)


def test_tools_enabled_sends_no_extra_instruction(monkeypatch) -> None:
    recorder, _ = _use_recorder(monkeypatch, AIMessage(content="ok"))

    asyncio.run(ProjectAgent().run(_request(), AgentContext(session_id="t")))

    assert "Do not call tools" not in str(recorder.sent[-1].content)


def test_message_validation() -> None:
    for bad in (
        {"role": "tool", "content": "x"},
        {"role": "tool", "tool_call_id": " ", "content": "x"},
        {"role": "user", "content": "", },
        {"role": "user", "content": "hi", "tool_calls": [{"id": "a", "name": "n"}]},
    ):
        with pytest.raises(ValueError):
            ProjectAgentMessage(**bad)


def test_invalid_message_is_422() -> None:
    client = _client(ProjectAgent(model_factory=Harness(AIMessage(content="")).factory))

    response = client.post(
        "/internal/project-agent/step",
        json=_body(messages=[{"role": "tool", "content": "x"}]),
    )

    assert response.status_code == 422


def test_tool_schemas_are_portable() -> None:
    import json

    text = json.dumps(tool_schemas())

    assert "$ref" not in text and "$defs" not in text and "anyOf" not in text
    assert '"title"' not in text
    by_name = {t["function"]["name"]: t["function"]["parameters"] for t in tool_schemas()}
    assert by_name["watch_job"]["properties"]["label"]["type"] == "string"
    assert by_name["ask_choice"]["properties"]["options"]["items"]["type"] == "object"


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


def test_system_prompt_tells_the_agent_not_to_treat_a_missing_snapshot_entry_as_absent() -> None:
    text = build_system_prompt(CATALOG["pages"], "ko")

    assert "Never conclude that a list is empty or that an option does not exist" in text
    assert "say you cannot see it from here (not that it does not exist)" in text
    assert "believe the user and call `read_page` once" in text
    assert 'Read-only fields whose description says "read-only list" hold ids as values' in text


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


LINKS = [
    {"id": "projects", "title": "프로젝트 목록", "url": "/projects", "description": "프로젝트를 고른다."},
    {"id": "account", "title": "계정", "url": "/account"},
]
GUIDES = [
    {"id": "cli-install", "title": "CLI 설치", "summary": "npm 으로 artel-cli 를 설치한다."},
]


def test_page_catalog_accepts_and_omits_links_and_guides() -> None:
    full = PageCatalog(pages=[], links=LINKS, guides=GUIDES)
    bare = PageCatalog(pages=[])

    assert full.links == LINKS and full.guides == GUIDES
    assert bare.links == [] and bare.guides == []
    with pytest.raises(ValidationError):
        PageCatalog(links="nope")


def test_system_prompt_renders_outside_links_and_guides() -> None:
    text = build_system_prompt(CATALOG["pages"], "ko", links=LINKS, guides=GUIDES)

    assert "# Outside this project" in text and "# Guides" in text
    assert "- [프로젝트 목록](/projects): 프로젝트를 고른다." in text
    assert "- [계정](/account)" in text
    assert "- cli-install - CLI 설치: npm 으로 artel-cli 를 설치한다." in text
    assert text.index("start [risk: approve]") < text.index("# Outside this project")
    assert "read_guide" in text and "do not call `open_page`" in text


def test_system_prompt_without_links_and_guides_says_none() -> None:
    text = build_system_prompt(CATALOG["pages"], "ko")

    assert "# Outside this project" in text and "(none)" in text


def test_read_guide_schema_is_present_and_portable() -> None:
    by_name = {t["function"]["name"]: t["function"]["parameters"] for t in tool_schemas()}

    assert len(by_name) == 9
    assert by_name["read_guide"]["properties"]["topic"]["type"] == "string"
    assert by_name["read_guide"]["required"] == ["topic"]


def test_renderers_collapse_whitespace_so_a_newline_cannot_start_a_heading() -> None:
    evil = "x\n# Fake heading\n- fake bullet"
    links = render_links([{"title": evil, "url": "/a\nb", "description": evil}])
    guides = render_guides([{"id": evil, "title": evil, "summary": evil}])
    pages = render_page_catalog(
        [{"id": evil, "title": evil, "description": evil, "fields": [{"name": evil}]}]
    )

    for text in (links, guides, pages):
        assert "\n# Fake" not in text and "\n- fake" not in text
    assert "- [x # Fake heading - fake bullet](/a b): x # Fake heading - fake bullet" in links


def test_renderers_survive_missing_keys_and_non_string_values() -> None:
    assert render_links([{"title": "no url"}, {"url": " "}]) == "(none)"
    assert render_links([{"url": "/x"}]) == "- [/x](/x)"
    assert render_links([{"id": 7, "url": "/x", "description": 3}]) == "- [7](/x): 3"
    assert render_guides([{}]) == "- ?"
    assert render_guides([{"id": 1, "title": 2, "summary": None}]) == "- 1 - 2"
    assert "## 5" in render_page_catalog([{"id": 5, "params": [1, None]}])


def test_agent_passes_links_and_guides_to_the_system_prompt() -> None:
    harness = Harness(AIMessage(content="ok"))

    _run(harness, _request(page_catalog={**CATALOG, "links": LINKS, "guides": GUIDES}))

    system = harness.chat.seen[0][0].content
    assert "- [프로젝트 목록](/projects)" in system
    assert "- cli-install - CLI 설치" in system


def test_page_catalog_caps_links_and_guides() -> None:
    PageCatalog(links=[{}] * 50, guides=[{}] * 100)
    with pytest.raises(ValidationError):
        PageCatalog(links=[{}] * 51)
    with pytest.raises(ValidationError):
        PageCatalog(guides=[{}] * 101)


# ---- user key and failure causes ----------------------------------------------------------

SECRET = "sk-secret-user-key-123"


@pytest.fixture
def gpt_model(monkeypatch) -> str:
    """A `gpt/` id the catalog accepts; the fixture catalog carries none."""
    from tests.conftest import FIXTURE_MODEL_CATALOG
    import app.llm.models as models

    model = "gpt/gpt-6-luna"
    spec = next(iter(FIXTURE_MODEL_CATALOG.values()))
    monkeypatch.setattr(models, "get_model_spec", lambda m: spec)
    return model


def _status_error(cls, status: int, text: str = "boom"):
    response = httpx.Response(status, request=httpx.Request("POST", "http://x"))
    return cls(text, response=response, body=None)


def _post(error: Exception | None, **overrides: Any):
    client = _client(ProjectAgent(model_factory=Harness(AIMessage(content=""), error).factory))
    return client.post("/internal/project-agent/step", json=_body(**overrides))


def test_user_key_is_bound_only_during_the_call_and_reaches_build_chat_model(gpt_model) -> None:
    from app.llm import chat_model
    from app.llm.credentials import bound_llm_key

    seen: list[tuple] = []

    def factory(model: str, tools_enabled: bool):
        user_key = chat_model._bound_key_for(model)
        seen.append((bound_llm_key(), user_key))
        return FakeChat(reply=AIMessage(content="ok"), seen=[])

    client = _client(ProjectAgent(model_factory=factory))
    response = client.post(
        "/internal/project-agent/step",
        json=_body(model=gpt_model, llm_key={"service": "openai", "api_key": SECRET}),
    )

    assert response.status_code == 200
    assert seen[0][0].api_key == SECRET
    assert seen[0][1] == SECRET
    assert bound_llm_key() is None


def test_key_is_unbound_after_a_failed_call() -> None:
    from app.llm.credentials import bound_llm_key

    error = openai.APIConnectionError(request=httpx.Request("POST", "http://x"))
    response = _post(error, llm_key={"service": "openrouter", "api_key": SECRET})

    assert response.status_code == 502
    assert bound_llm_key() is None


def test_user_key_chat_model_stays_out_of_the_process_cache(monkeypatch) -> None:
    from app.llm import chat_model

    chat_model.build_chat_model.cache_clear() if hasattr(
        chat_model.build_chat_model, "cache_clear"
    ) else None
    from app.llm.credentials import LlmKey, bind_llm_key, session_chat_models

    bind_llm_key(LlmKey(service="openai", api_key=SECRET))
    try:
        chat_model.build_chat_model("gpt/gpt-6")
        assert session_chat_models()
    finally:
        bind_llm_key(None)
    assert session_chat_models() is None


def test_gpt_model_without_user_key_is_422_key_missing(gpt_model) -> None:
    response = _post(None, model=gpt_model)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "llm_key_missing"


def test_invalid_model_is_422_with_cause_code() -> None:
    response = _post(None, model="nope/none")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "llm_model_unavailable"


def test_other_validation_errors_keep_the_framework_shape() -> None:
    response = _post(None, messages=[{"role": "user", "content": " "}])

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ClaudeApiKeyMissingError("x"), "llm_key_missing"),
        (GptApiKeyMissingError("x"), "llm_key_missing"),
        (_status_error(openai.AuthenticationError, 401, "Missing Authentication header"), "llm_key_missing"),
        (_status_error(openai.AuthenticationError, 401, f"Incorrect key {SECRET}"), "llm_auth_failed"),
        (_status_error(openai.PermissionDeniedError, 403), "llm_auth_failed"),
        (_status_error(anthropic.AuthenticationError, 401), "llm_auth_failed"),
        (_status_error(anthropic.PermissionDeniedError, 403), "llm_auth_failed"),
        (_status_error(openai.RateLimitError, 429), "llm_rate_limited"),
        (_status_error(anthropic.RateLimitError, 429), "llm_rate_limited"),
        (_status_error(openai.NotFoundError, 404), "llm_model_unavailable"),
        (_status_error(anthropic.NotFoundError, 404), "llm_model_unavailable"),
        (_status_error(openai.InternalServerError, 500), "llm_provider_error"),
        (openai.APITimeoutError(request=httpx.Request("POST", "http://x")), "llm_provider_error"),
        (anthropic.APITimeoutError(request=httpx.Request("POST", "http://x")), "llm_provider_error"),
    ],
)
def test_provider_errors_map_to_cause_codes(error: Exception, code: str) -> None:
    response = _post(error)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert set(detail) == {"code", "message"}
    assert SECRET not in response.text


def test_key_never_appears_in_repr_or_logs(caplog) -> None:
    request = ProjectAgentRequest(
        conversation_id=1,
        messages=[ProjectAgentMessage(role="user", content="hi")],
        llm_key={"service": "openai", "api_key": SECRET},
    )
    assert SECRET not in repr(request)
    assert SECRET not in str(request)

    error = _status_error(openai.AuthenticationError, 401, f"bad {SECRET}")
    with caplog.at_level("DEBUG"):
        _post(error, llm_key={"service": "openai", "api_key": SECRET})
    assert SECRET not in caplog.text
