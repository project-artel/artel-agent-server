from typing import Literal

from pydantic import BaseModel, Field

from app.agents import ScenarioDraft
from app.qa.envelope import GameState, QaChatTurn
from app.qa.run_config import RunConfig


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
    # Settled at open, not at run start: the run has to be attributable from the
    # moment the session exists, and the open response is what carries it back to
    # Orchestration. Nothing here is re-decided later.
    run_config: RunConfig

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
