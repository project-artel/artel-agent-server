from typing import Literal

from pydantic import BaseModel, Field

from app.agents import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
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
    # Output locale for generated scenarios. Default keeps records saved
    # before this field was introduced deserializing as Korean.
    locale: OutputLanguage = DEFAULT_LANGUAGE
    # What the session's LLM spend is booked against. Optional because
    # Orchestration may not be sending it yet; then the usage record carries a
    # null reference instead of the wrong one.
    test_scenario_id: int | None = None
