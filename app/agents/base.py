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

    def trace_config(self, run_name: str) -> dict[str, Any]:
        """Build the shared LangChain config used to identify a top-level run."""
        return {
            "run_name": run_name,
            "tags": ["agent"],
            "metadata": {**self.metadata, "session_id": self.session_id},
        }


class BaseAgent(Protocol[RequestT, ResultT]):
    async def run(self, request: RequestT, context: AgentContext) -> ResultT:
        """Run the agent with a typed request and shared execution context."""
