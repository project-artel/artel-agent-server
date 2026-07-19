from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from app.llm.client import LLMClient


RequestT = TypeVar("RequestT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


@dataclass(frozen=True)
class AgentContext:
    session_id: str
    llm: LLMClient
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAgent(Protocol[RequestT, ResultT]):
    async def run(self, request: RequestT, context: AgentContext) -> ResultT:
        """Run the agent with a typed request and shared execution context."""
