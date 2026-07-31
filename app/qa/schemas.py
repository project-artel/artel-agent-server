from typing import Literal

from pydantic import BaseModel, Field

from app.agents import DEFAULT_LANGUAGE, OutputLanguage, ScenarioDraft
from app.llm.models import DEFAULT_MODEL, LLMModel, ReasoningConfig
from app.qa.envelope import GameState, QaChatTurn


class QaStepResult(BaseModel):
    step: int
    passed: bool
    message: str


# Phase of the per-step loop the session is currently in.
#   need_action -> waiting to plan/emit the ACTION for the current step
#   need_result -> ACTION sent, waiting for the ACTION_RESULT to verify
#   done        -> terminal (completed, failed, or cancelled)
QaPhase = Literal["need_action", "need_result", "done"]


class QaSessionRecord(BaseModel):
    # Frozen at session open (from POST /qa-sessions context).
    qa_try_id: int
    game_instance_id: int
    test_scenario_id: int
    # The approved test scenario the Agent executes step by step.
    scenario: ScenarioDraft
    model: LLMModel = DEFAULT_MODEL
    language: OutputLanguage = DEFAULT_LANGUAGE
    # Which prompt version this run uses. None defers to QA_PROMPT_VERSION, and
    # then to the newest version on disk.
    prompt_version: str | None = None
    reasoning: ReasoningConfig | None = None

    # Live execution state.
    current_step: int = 0  # 0-based index into scenario.steps
    phase: QaPhase = "need_action"
    sequence: int = 0  # monotonic per-session outbound counter
    latest_game_state: GameState | None = None
    last_action_message_id: str | None = None
    step_results: list[QaStepResult] = Field(default_factory=list)
    # Operator conversation. Trimmed to the most recent turns on every append:
    # the whole record is rewritten to Redis each turn, so it cannot grow without
    # bound, and only the recent turns are what steer the next decision.
    chat: list[QaChatTurn] = Field(default_factory=list)
