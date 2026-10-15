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
# pe1: 뼈대만 있는 엔진이다(ARTEL-1080). planner(LLM)가 step 마다 module 하나를 쓴다 — func 마다 Python
# `when` 과 macro DSL `run`, 그리고 `done`·`lost`·`check`. 답마다 sandbox, macro parser, `check` 규칙으로
# 검사하고, 거절되면 이유를 실어 `max_rewrites` 번까지 다시 쓰게 한다. executer 는 turn 마다 `when`
# 이 참인 func 가운데 `FUNCS` 에 먼저 적힌 것을 macro runner 로 돌린다. 입력과 macro 의 `ask`·
# `decide` 는 tool loop 의 macro 와 같은 runner 와 같은 Jev client 로 가서, 두 구조를 비교할 때
# 게임에 닿는 층이 같다. evaluator 는 play 가 멈춘 뒤 `check` 를 돌려 step 을 판정한다. step module
# 편집, play 검토, 진척 측정, Jev 로 func 고르기, 게임 노트, game time 붙잡기는 없다.
PLANNER_EXECUTER_LABEL = "pe1-skeleton"

# Which facts `planner_executer_fingerprint` hashes. Bump when that set changes.
_FINGERPRINT_SCHEME = 1


class PlannerExecuterArchSpec(BaseModel):
    """The knobs of a planner–executer run. Every field optional; the defaults are the engine."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(default=PLANNER_EXECUTER_LABEL, max_length=50)
    # Turns and wall-clock one step may take. A step can be a whole level.
    max_turns_per_step: int = Field(default=300, ge=1, le=10_000)
    step_budget_seconds: float = Field(default=480.0, gt=0, le=3_600)
    # 어떤 func 의 `when` 도 참이 아닌 상태가 이만큼 이어지면 play 를 멈추고 판정한다. 카운트다운,
    # 팝업, 장면 전환은 1–2초에 지나간다.
    no_func_grace_seconds: float = Field(default=3.0, gt=0, le=120)
    # func 의 macro 가 이만큼 연달아 실패하면 play 를 멈추고 판정한다.
    max_failures_in_a_row: int = Field(default=3, ge=1, le=50)
    # planner 의 답이 거절될 때 다시 쓰게 하는 횟수. 다 쓰고도 거절되면 그 step 은 FAILED 다.
    max_rewrites: int = Field(default=2, ge=0, le=5)


def planner_executer_fingerprint(spec: PlannerExecuterArchSpec, functions: tuple[str, ...]) -> str:
    """A digest of the engine's structure: its knobs and the functions a step module can use.

    `functions` are what a step module can use: the names on `g` and the macro tools its funcs
    send (`run_config.step_module_functions`). A function added or removed changes what the
    planner can write, so it is a different engine to the planner. Model, prompt version and
    label are excluded for the reasons `arch_fingerprint` gives.
    """
    facts = {
        "scheme": _FINGERPRINT_SCHEME,
        "kind": "planner-executer",
        "planner": spec.model_dump(mode="json", exclude={"label"}),
        "functions": sorted(functions),
    }
    digest = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(digest.encode()).hexdigest()[:12]
