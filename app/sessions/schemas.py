from typing import Literal

from pydantic import BaseModel, Field

from app.agents import DEFAULT_LANGUAGE, OutputLanguage, ScenarioPlan
from app.llm.models import DEFAULT_MODEL, LLMModel


class HistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    message: str
    # Assistant turns keep the scenarios they authored (audit/restore); these are
    # NOT replayed into the prompt — only `message` text is. Multi-scenario plans
    # now (ARTEL-206/227), where a turn once kept one step-based ScenarioDraft.
    scenarios: list[ScenarioPlan] | None = None


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
    # Run scope (ARTEL-206). Set when the authoring session is opened from a run
    # dashboard, so the agent's case search and the orchestration-side reconcile
    # can be bound to the right run/project. Optional: callers that predate run
    # scope (and existing tests) omit them.
    run_id: int | None = None
    project_id: int | None = None
