from typing import Literal

from pydantic import BaseModel, Field

from app.qa.envelope import GameState, QaChatTurn
from app.qa.run_config import RunConfig


class QaCaseRef(BaseModel):
    """실행 계약에서 스텝에 동봉되는 TC 리졸브(재설계 2026-08-07).

    필드명은 TC 스펙(CSV: 씬/사전조건/테스트스텝/기대결과/근거)을 미러링한다. 판정 기준은
    `precondition`(구간 진입 시 충족 검증)과 `expected`(구간 끝 판정)다.
    """

    id: int
    scene: str | None = None
    precondition: str | None = None
    test_step: str | None = None
    expected: str = ""


class QaStep(BaseModel):
    """시나리오 스텝 하나 = 행위 하나. `case_id`가 있으면 그 스텝은 해당 TC 검증 구간에 속한다
    (연속 동일 case_id = 한 TC 구간). 없으면 단순 행위(판정 없음). `case`는 case_id의 TC 리졸브."""

    action: str = ""
    case_id: int | None = None
    hint: str | None = None
    input: str | None = None
    case: QaCaseRef | None = None


class QaScenario(BaseModel):
    """Agent가 실행하는 시나리오 = 순서 있는 Step 리스트(재설계). Orche 실행 계약과 동일 형태."""

    title: str = ""
    description: str = ""
    steps: list[QaStep] = Field(default_factory=list)


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
    # The approved test scenario the Agent executes: an ordered Step list (재설계).
    scenario: QaScenario
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
