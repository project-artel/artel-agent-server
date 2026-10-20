"""Request and result of one project agent step.

The agent is stateless. Orchestration stores the conversation, sends all of it
on every call, executes the tool calls that come back through the browser, and
calls again. See `.plan/general/2026-10-20-project-agent-wire-contract.md`.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.llm.models import EnabledLLMModel


class ProjectAgentToolCall(BaseModel):
    id: str
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class ProjectAgentMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str = ""
    # Only on an assistant message: the calls it made.
    tool_calls: list[ProjectAgentToolCall] = Field(default_factory=list)
    # Only on a tool message: which call it answers.
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def check_role_fields(self) -> "ProjectAgentMessage":
        if self.role == "tool" and not (self.tool_call_id or "").strip():
            raise ValueError("A tool message needs a non-empty tool_call_id.")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("tool_calls are allowed only on assistant messages.")
        if self.role == "user" and not self.content.strip():
            raise ValueError("A user message needs non-empty content.")
        return self


class PageCatalog(BaseModel):
    """Opaque JSON from the browser.

    Checked: `pages`, `links` and `guides` are lists of objects, and `links` and
    `guides` are capped at 50 and 100 entries. Keys inside each entry are not checked.
    """

    model_config = {"extra": "allow"}

    pages: list[dict[str, Any]] = Field(default_factory=list)
    # Console pages outside the project: {id, title, url, description}.
    links: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    # How-to documents: {id, title, summary}. Bodies come from the read_guide tool.
    guides: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class ProjectAgentRequest(BaseModel):
    conversation_id: int
    project_id: int | None = None
    # None means DEFAULT_MODEL. An unknown or disabled id is a 422.
    model: EnabledLLMModel | None = None
    locale: str = "ko"
    tools_enabled: bool = True
    page_catalog: PageCatalog = Field(default_factory=PageCatalog)
    messages: list[ProjectAgentMessage]


class ProjectAgentResult(BaseModel):
    content: str = ""
    tool_calls: list[ProjectAgentToolCall] = Field(default_factory=list)
