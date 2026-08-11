from pydantic import BaseModel, Field

from app.qa.envelope import QaChatTurn
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
    """스텝 하나의 판정(2단 판정 2026-08-08). **모든 스텝**에 성공/실패를 남긴다.

    검증 스텝(`is_verification`=구간의 마지막 스텝)은 기대결과 충족 여부, 그 외 스텝은
    그 행위를 수행/성립했는지를 판정한다. `case_id`는 이 스텝이 속한 TC(없으면 단순 행위).
    """

    step: int
    passed: bool
    message: str
    case_id: int | None = None
    is_verification: bool = False


class QaRunScenario(BaseModel):
    """런 안의 시나리오 하나 = 자기 qa_try_id + 실행 본문(스텝 리스트) (ARTEL-258).

    qa_try는 시나리오당이라 각 시나리오가 자기 `qa_try_id`를 들고 다닌다 — 프레임은 실행
    중인 시나리오의 try로 stamp된다. `scenario`는 그 시나리오의 실행 본문(재설계 Step 리스트).
    """

    qa_try_id: int
    test_scenario_id: int
    scenario: QaScenario


class QaSessionRecord(BaseModel):
    """QA_Run 세션 하나 = 런의 시나리오들을 순차 실행 (ARTEL-258/259).

    세션은 **런 단위**(WS 유지). qa_try·채널은 시나리오당이고, 시나리오 사이는 ResetPolicy가
    게임을 초기화한다. `run_config`는 open에서 확정한다 — 세션이 열리는 순간부터 귀속 가능해야
    하므로 여기서 한 번 결정하고 재결정하지 않는다.
    """

    # Frozen at session open (from POST /qa-sessions context).
    qa_run_id: int
    game_instance_id: int
    # The run's scenarios, executed in order — each carries its own qa_try_id.
    scenarios: list[QaRunScenario]
    # Settled at open, not at run start: the run has to be attributable from the
    # moment the session exists, and the open response is what carries it back to
    # Orchestration. Nothing here is re-decided later.
    run_config: RunConfig
    # Operator conversation (run-scoped). Trimmed to the most recent turns on every
    # append: the whole record is rewritten to Redis each turn, so it cannot grow
    # without bound, and only the recent turns are what steer the next decision.
    chat: list[QaChatTurn] = Field(default_factory=list)
