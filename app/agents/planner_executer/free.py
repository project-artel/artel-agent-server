"""FREE 시나리오의 case 를 씬 단위로 묶는 규칙(ARTEL-1149).

FREE 시나리오는 step 하나가 test case 하나이고 순서가 없다. case 마다 planner·evaluator 를 부르면 비용이
case 수에 비례한다 — stage 의 FREE 시나리오는 case 101개다. 그래서 씬에 들어가면 그 씬의 후보 case 를 묶어
planner 에게 한 번에 주고, planner 가 지금 검증할 case 와 순서를 정한다. 여기는 후보를 추리고 다음에 갈 씬을
고르는 일만 한다. 둘 다 LLM 을 쓰지 않고 씬 이름이 같은지만 본다 — case 의 `scene` 은 content map 의 씬
이름이고, 현재 씬은 SDK 의 reading 이 보내는 같은 이름이다.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.qa.schemas import QaScenario


@dataclass
class FreeCase:
    """FREE 시나리오의 case 하나와 그 case 를 몇 번 play 했는지."""

    # 시나리오의 step 번호(1 부터). planner·evaluator·보고가 모두 이 번호로 case 를 찾는다.
    step: int
    scene: str | None
    attempts: int = 0
    # 아직 보고하지 않은 이유: planner 가 `later` 로 둔 까닭, 닿지 못한 씬, 목표에 닿지 못한 play 의 판정.
    # 상한에 닿아 실패로 보고할 때 같이 적는다.
    last_reason: str = "not played"
    # `last_reason` 의 종류. UNREACHED 보고가 "씬에 못 갔다" 와 "play 했는데 목표에 못 닿았다" 를 가르는 값이다
    # (ARTEL-1186). `NOT_PLAYED`·`DEFERRED`·`LEFT_SCENE`·`SCENE_UNREACHED`·`GOAL_UNREACHED`.
    cause: str = "NOT_PLAYED"


def free_cases(scenario: QaScenario) -> list[FreeCase]:
    return [
        FreeCase(step=number, scene=step.case.scene if step.case else None)
        for number, step in enumerate(scenario.steps, start=1)
    ]


def same_scene(case_scene: str | None, current: str | None) -> bool:
    if not case_scene or not current:
        return False
    return case_scene.strip().casefold() == current.strip().casefold()


def has_known_scene(case: FreeCase, known: set[str]) -> bool:
    """The case names a scene the game map has. With no map, any named scene counts."""
    if not case.scene:
        return False
    return not known or case.scene.strip().casefold() in known


def candidates(pending: Iterable[FreeCase], scene: str | None, known: set[str], limit: int) -> list[FreeCase]:
    """The cases a plan for `scene` is offered, in the order written, at most `limit`: the scene's
    own cases first, then cases whose scene is not known — those may be checkable anywhere."""
    pending = list(pending)
    own = [case for case in pending if same_scene(case.scene, scene)]
    unplaced = [case for case in pending if not has_known_scene(case, known) and case not in own]
    return (own + unplaced)[:limit]


def next_scene(pending: Iterable[FreeCase], current: str | None, known: set[str]) -> FreeCase | None:
    """The first case, in the order written, whose scene is known and is not the one on screen."""
    for case in pending:
        if has_known_scene(case, known) and not same_scene(case.scene, current):
            return case
    return None
