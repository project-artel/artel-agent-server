from typing import Literal

from pydantic import BaseModel, Field

from app.agents import (
    DEFAULT_LANGUAGE,
    OutputLanguage,
    ScenarioPlan,
    AuthoredFlow,
    TestCaseListItem,
)
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
    # Every TestCase in the project (ARTEL-319), also frozen at open. Kept on
    # the record rather than re-fetched per turn so the prompt's cached prefix
    # stays byte-identical for the life of the session.
    test_case_list: list[TestCaseListItem] = Field(default_factory=list)
    # Walkable flows for this session (ARTEL-658). A snapshot like the case list —
    # both come from the same map read when the session opens.
    flows: list[AuthoredFlow] = Field(default_factory=list)
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
    # Run scope (ARTEL-206). Set when the authoring session is opened from a run
    # dashboard, so the agent's case search and the orchestration-side reconcile
    # can be bound to the right run/project. Optional: callers that predate run
    # scope (and existing tests) omit them.
    run_id: int | None = None
    project_id: int | None = None
    # The run's current scenarios (ARTEL-206 Step 6), refreshed every turn by the
    # orchestration server. Fed to the agent so it can target existing scenarios
    # for edits (echoing `scenario_id`). Default empty keeps older records valid.
    current_scenarios: list[ScenarioPlan] = Field(default_factory=list)
