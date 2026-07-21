from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar


RequestT = TypeVar("RequestT", contravariant=True)
ResultT = TypeVar("ResultT", covariant=True)


@dataclass(frozen=True)
class AgentContext:
    # Correlation id for logging/tracing/metrics. Not a state key — session
    # state lives in the session layer.
    session_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseAgent(Protocol[RequestT, ResultT]):
    async def run(self, request: RequestT, context: AgentContext) -> ResultT:
        """Run the agent with a typed request and shared execution context."""
