import uuid
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.project_agent.prompt import build_system_prompt
from app.agents.project_agent.schemas import (
    ProjectAgentMessage,
    ProjectAgentRequest,
    ProjectAgentResult,
    ProjectAgentToolCall,
)
from app.agents.project_agent.tools import tool_schemas
from app.llm.chat_model import build_chat_model
from app.llm.models import DEFAULT_MODEL

ModelFactory = Callable[[str, bool], Runnable]


def _default_model_factory(model: str, tools_enabled: bool) -> Runnable:
    chat: BaseChatModel = build_chat_model(model)
    return chat.bind_tools(tool_schemas()) if tools_enabled else chat


def to_langchain_messages(messages: list[ProjectAgentMessage]) -> list[BaseMessage]:
    converted: list[BaseMessage] = []
    for message in messages:
        if message.role == "user":
            converted.append(HumanMessage(content=message.content))
        elif message.role == "assistant":
            converted.append(
                AIMessage(
                    content=message.content,
                    tool_calls=[
                        {"id": call.id, "name": call.name, "args": call.args, "type": "tool_call"}
                        for call in message.tool_calls
                    ],
                )
            )
        else:
            converted.append(
                ToolMessage(content=message.content, tool_call_id=message.tool_call_id or "")
            )
    return converted


def flatten_content(content: Any) -> str:
    """Provider text as one string. Block lists keep their text blocks and drop the rest."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


class ProjectAgent:
    """One model call. Stateless: no memory, no loop, no tool execution.

    ``model_factory`` is injectable so tests can supply a fake chat model.
    """

    def __init__(self, model_factory: ModelFactory | None = None) -> None:
        self._model_factory = model_factory or _default_model_factory

    async def run(
        self, request: ProjectAgentRequest, context: AgentContext
    ) -> ProjectAgentResult:
        model = request.model or DEFAULT_MODEL
        chat = self._model_factory(model, request.tools_enabled)
        system = build_system_prompt(request.page_catalog.pages, request.locale)
        reply = await chat.ainvoke(
            [SystemMessage(content=system), *to_langchain_messages(request.messages)],
            context.trace_config("project-agent"),
        )
        tool_calls = [
            ProjectAgentToolCall(
                id=call.get("id") or f"call_{uuid.uuid4().hex}",
                name=call["name"],
                args=call.get("args") or {},
            )
            for call in getattr(reply, "tool_calls", None) or []
        ]
        return ProjectAgentResult(content=flatten_content(reply.content), tool_calls=tool_calls)
