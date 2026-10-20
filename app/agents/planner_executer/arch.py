"""What shape the planner–executer engine has, recorded the way `app/agents/qa/arch.py` records
the tool loop's: a label to read, and a fingerprint derived from the knobs.

The knobs live on a spec of their own, `QaArchSpec.planner`, rather than beside the tool loop's.
The two engines share no structure, so a knob added to one must not move the other's digest.
"""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field

# Bumped by hand when the engine's shape changes in a way the fingerprint does not see — what the
# planner reads, or how the executer decides between functions — the same rule as
# `QA_ARCH_LABEL`. Counted apart from it: the two engines' labels name different things.
#
# pe1: 뼈대만 있는 엔진이다(ARTEL-1080). planner(run 의 LLM)가 step 마다 macro DSL 글 하나를 쓴다 —
# docstring 이 달린 action macro 여럿과 목표 predicate `done`. macro parser 가 받지 않으면 parser 의
# 문장을 실어 `max_rewrites` 번까지 다시 쓰게 한다. executer 는 Jev 로 돈다: turn 마다 `done` 을 읽고,
# 참이면 Jev 의 `ask` 로 맞는지 다시 묻고, 아니면 Jev 의 `decide` 로 action 하나를 골라 macro runner 로
# 돌린다. Jev 요청은 macro 의 `ask`·`decide` 와 같은 모양으로 같은 client 에 보낸다. evaluator(LLM)는
# play 가 멈춘 뒤 turn 기록과 step 처음·끝의 화면을 읽고 판정한다. teammate 코드는 macro 언어
# (parser 와 runner)만 그대로 쓴다.
#
# pe2: FREE 시나리오를 돈다(ARTEL-1149). FREE 시나리오는 씬 단위로 돈다: 씬에 들어가면 planner 가 그 씬의
# 후보 case(최대 `free_batch_size`)를 보고 지금 검증할 case 와 순서, 공용 action 과 case 마다 목표
# `done_<case_id>()` 를 한 번에 쓴다(prompt `scene_rules`). executer 는 씬이 그대로인 동안 그 순서로 case 를 하나씩
# 돌고, 판정은 다섯 개씩 묶어 묻는다(`scene_evaluator`). 후보가 한 계획보다 많은 씬은 남은 후보로 다시 계획한다. 다음 씬으로는 planner 가 이동 step 을 쓴다. 미룬 case 는 라운드가
# 끝나면 `reset_game` 뒤 다시 돌고, 상한(`free_max_resets`, `free_case_attempts`) 뒤 남은 case 는 UNREACHED 로
# 실패 보고한다. ORDERED 시나리오의 루프는 그대로지만 knob 이 늘어 fingerprint 가 바뀐다.
PLANNER_EXECUTER_LABEL = "pe2-free"

# Which facts `planner_executer_fingerprint` hashes. Bump when that set changes.
_FINGERPRINT_SCHEME = 1


class PlannerExecuterArchSpec(BaseModel):
    """The knobs of a planner–executer run. Every field optional; the defaults are the engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(default=PLANNER_EXECUTER_LABEL, max_length=50)
    # Turns and wall-clock one step may take. A step can be a whole level.
    max_turns_per_step: int = Field(default=300, ge=1, le=10_000)
    step_budget_seconds: float = Field(default=480.0, gt=0, le=3_600)
    # Jev 가 `none_fits` 를 고르는 상태가 이만큼 이어지면 play 를 멈춘다. 카운트다운, 팝업, 장면 전환은
    # 1–2초에 지나간다.
    no_action_grace_seconds: float = Field(default=3.0, gt=0, le=120)
    # action 의 macro 가 이만큼 연달아 실패하면 play 를 멈춘다.
    max_failures_in_a_row: int = Field(default=3, ge=1, le=50)
    # `done()` 이 참인데 Jev 가 목표에 닿지 않았다고 본 횟수가 이만큼이면 play 를 멈춘다.
    max_done_disagreements: int = Field(default=3, ge=1, le=50)
    # planner 의 답이 거절될 때 다시 쓰게 하는 횟수. 다 쓰고도 거절되면 그 step 은 FAILED 다.
    max_rewrites: int = Field(default=2, ge=0, le=5)
    # FREE 시나리오: 한 라운드가 끝나고 미룬 case 가 남았을 때 `reset_game` 으로 다시 시작하는 횟수.
    free_max_resets: int = Field(default=1, ge=0, le=10)
    # FREE 시나리오: case 하나를 돌아 보는 횟수. 목표에 닿지 못한 실패는 이 횟수까지 미룬다.
    free_case_attempts: int = Field(default=2, ge=1, le=10)
    # FREE 시나리오: 씬 계획 한 번에 planner 에게 주는 후보 case 의 최대 수.
    free_batch_size: int = Field(default=20, ge=1, le=100)


def planner_executer_fingerprint(spec: PlannerExecuterArchSpec, functions: tuple[str, ...]) -> str:
    """A digest of the engine's structure: its knobs and the macro tools the planner can write.

    A tool added or removed changes what the planner can write, so it is a different engine to
    the planner. Model, prompt version and label are excluded for the reasons `arch_fingerprint`
    gives.
    """
    facts = {
        "scheme": _FINGERPRINT_SCHEME,
        "kind": "planner-executer",
        "planner": spec.model_dump(mode="json", exclude={"label"}),
        "functions": sorted(functions),
    }
    digest = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(digest.encode()).hexdigest()[:12]
