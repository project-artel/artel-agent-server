from typing import Literal

from pydantic import BaseModel, Field

from app.agents import ScenarioDraft
from app.llm.models import DEFAULT_MODEL, LLMModel


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    message: str
    # Assistant turns keep the full generated draft (audit/restore); this is NOT
    # replayed into the prompt — only `message` text is.
    scenario: ScenarioDraft | None = None


class SessionRecord(BaseModel):
    # Frozen at session open (Unity SDK snapshot + user-provided game context).
    unity_context: dict = Field(default_factory=dict)
    game_context: dict = Field(default_factory=dict)
    # Full conversation turns; windowed for prompt reconstruction by the service.
    history: list[HistoryTurn] = Field(default_factory=list)
    # First user input, consumed when the WS connects to run the first turn.
    pending_user_input: str | None = None
    model: LLMModel = DEFAULT_MODEL
