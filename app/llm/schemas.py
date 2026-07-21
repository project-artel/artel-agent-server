from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(StrEnum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class LLMMessage(BaseModel):
    role: MessageRole
    content: str


class LLMRequest(BaseModel):
    model: str
    messages: list[LLMMessage]
    temperature: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, gt=0)
    response_format: dict[str, Any] | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)

    def to_chat_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            exclude_none=True,
            exclude={"extra_body"},
            mode="json",
        )
        payload.update(self.extra_body)
        return payload


class LLMResponse(BaseModel):
    model: str
    content: str
    id: str | None = None
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
