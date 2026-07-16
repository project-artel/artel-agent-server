from abc import ABC, abstractmethod

from app.llm.schemas import LLMRequest, LLMResponse


class LLMClient(ABC):
    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return a single non-streaming LLM completion."""
