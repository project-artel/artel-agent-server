from pydantic import BaseModel, Field

from app.qa.cases import QaScenarioBody
from app.qa.envelope import QaChatTurn
from app.qa.run_config import RunConfig


class QaStepResult(BaseModel):
    step: int
    passed: bool
    message: str


class QaScenario(BaseModel):
    """런 안의 시나리오 하나 = 자기 qa_try_id + 실행 본문 (ARTEL-258).

    qa_try는 시나리오당이라 각 시나리오가 자기 [qa_try_id]를 들고 다닌다 — 프레임은 실행 중인
    시나리오의 try로 stamp된다. [scenario]는 cases(저작 Step)나 레거시 steps를 담는다.
    """

    qa_try_id: int
    test_scenario_id: int
    scenario: QaScenarioBody


class QaSessionRecord(BaseModel):
    """QA_Run 세션 하나 = 런의 시나리오들을 순차 실행 (ARTEL-258/259).

    세션은 **런 단위**(WS 유지). qa_try·채널은 시나리오당이고, 시나리오 사이는 ResetPolicy가
    게임을 초기화한다. [run_config]는 open에서 확정한다 — 세션이 열리는 순간부터 귀속 가능해야
    하므로 여기서 한 번 결정하고 재결정하지 않는다.
    """

    qa_run_id: int
    game_instance_id: int
    scenarios: list[QaScenario]
    run_config: RunConfig
    # 운영자 대화(런 단위). 최근 턴만 유지 — 레코드는 매 턴 통째로 저장되므로 무한 성장하면 안 된다.
    chat: list[QaChatTurn] = Field(default_factory=list)
