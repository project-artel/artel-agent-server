"""FREE 시나리오에서 다음에 돌 test case 를 고르는 규칙.

FREE 시나리오는 step 하나가 test case 하나이고 순서가 없다(`_step_plan` 이 step 마다 `is_verification` 을
세운다). 사람 QA 처럼, 지금 화면의 씬에서 검증할 수 있는 case 를 먼저 돈다 — 다른 씬의 case 를 먼저 고르면
그 씬까지 갔다가 돌아오는 데 step 예산을 쓴다. 씬이 맞는 case 가 없으면 시나리오에 적힌 순서의 첫 case 다.

고르는 데 LLM 을 쓰지 않는다. 씬 이름이 같은지만 본다 — case 의 `scene` 은 content map 의 씬 이름이고, 현재
씬은 SDK 의 reading 이 보내는 같은 이름이다.
"""

from dataclasses import dataclass

from app.qa.schemas import QaScenario


@dataclass
class FreeCase:
    """FREE 시나리오의 case 하나와 그 case 를 몇 번 돌았는지."""

    # 시나리오의 step 번호(1 부터). planner·evaluator·보고가 모두 이 번호로 case 를 찾는다.
    step: int
    scene: str | None
    attempts: int = 0
    # 미뤘을 때 마지막 판정의 이유. 상한에 닿아 실패로 보고할 때 같이 적는다.
    last_reason: str = ""


def free_cases(scenario: QaScenario) -> list[FreeCase]:
    return [
        FreeCase(step=number, scene=step.case.scene if step.case else None)
        for number, step in enumerate(scenario.steps, start=1)
    ]


def same_scene(case_scene: str | None, current: str | None) -> bool:
    if not case_scene or not current:
        return False
    return case_scene.strip().casefold() == current.strip().casefold()


def pick_case(pending: list[FreeCase], current_scene: str | None) -> FreeCase:
    """`pending` 에서 다음에 돌 case. 현재 씬의 case 가 먼저, 없으면 적힌 순서의 첫 case."""
    if not pending:
        raise ValueError("no case left to pick")
    return next((case for case in pending if same_scene(case.scene, current_scene)), pending[0])
