from pydantic import BaseModel, Field

from app.llm.models import DEFAULT_MODEL, LLMModel
from app.llm.schemas import LLMMessage


class SessionRecord(BaseModel):
    # Frozen at session open (Unity SDK snapshot + user-provided game context).
    unity_context: dict = Field(default_factory=dict)
    game_context: dict = Field(default_factory=dict)
    # Recent chat turns kept for prompt reconstruction (windowed by the service).
    history: list[LLMMessage] = Field(default_factory=list)
    # First user input, consumed when the WS connects to run the first turn.
    pending_user_input: str | None = None
    model: LLMModel = DEFAULT_MODEL
