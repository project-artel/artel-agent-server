"""What shape the planner–executer engine has, recorded the way `app/agents/qa/arch.py` records
the tool loop's: a label to read, and a fingerprint derived from the knobs.

The knobs live on a spec of their own, `QaArchSpec.planner`, rather than beside the tool loop's.
The two engines share no structure, so a knob added to one must not move the other's digest.
"""

import hashlib
import json

from typing import Literal

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
#
# pe3: executer 가 Jev 에게 묻는 모양을 JEV-Star 처럼 바꿨다. 선택지는 지금 할 수 있는 action 만이고(planner 가 쓴
# `can_<action>()` 이 거짓인 것은 뺀다), 각 선택지는 docstring 전문이 아니라 첫 줄(developer macro 는 줄 끝 주석)이다.
# 질문에 판단 순서(입력 막힘 → 진전 → 기다림)를 적고, state 에 최근 선택과 결과(`recent_picks`)를 싣는다. Jev 가
# 돌려준 선택지별 확률과 confidence 는 turn 기록에 남긴다. 로컬 try 184 에서 Jev 는 `end_turn` docstring 의
# "GameClearScene" 에 끌려 공격 없이 턴 종료만 11번 골랐고, 재현에서 이 모양으로 바꾸자 시전·턴 종료를 제때 골랐다.
#
# pe4: planner 가 판단용 특징값 `feature_<name>()` 을 쓰면, executer 가 턴마다 계산해 화면 오브젝트 목록 대신 Jev 에게
# 보낸다(scene, features, recent_picks). 로컬 try 185 에서 pe3 의 거른 오브젝트 6개만 받은 Jev 가 손패가 2장에서
# 22장까지 쌓이는 동안 시전을 0.01~0.10 으로 매겼다. 손패 수 같은 값을 준 재현에서는 시전을 33/33 골랐다.
#
# pe5: Jev 와 planner 가 읽는 값의 전처리를 고쳤다. SDK 가 목록·배열을 개수만 싣는 member(`count`)가 화면에는
# `None` 으로, Jev 에게는 `null` 로 갔고 `member(..., "X.Count")` 는 거절됐다. 이제 `<이름>.Count` 로 그리고 읽는다.
# 거르지 않는 state(`raw`)와 목표 확인(`goal_ask`)은 경로 알파벳순 대신 계획이 부르는 object 를 앞에 두고 자른다.
# try 192 를 다시 쌓은 감사에서 개수 827건이 어디에도 나가지 않았고, raw state 는 live object 의 72% 를 잘랐다.
#
# pe6: Jev state 를 만드는 규칙을 하나로 세웠다(`jev_state.assemble`). 선택지 action 과 판단 macro 가 읽는 object·
# member·static(`Reads`, developer macro 몸통까지)을 반드시 싣고, feature 는 object 목록을 대신하지 않고 보태며,
# static 을 처음으로 싣는다. `recent_picks` 에는 action 앞뒤로 바뀐 읽는 값(`changed`)을, turn 기록에는 보낸 state
# 전체를 남긴다. 이름표는 docstring 첫 줄이 아니라 첫 문장이다. try 193 에서 Jev 는 static 없이, 고정 0 인 11개
# object 와 줄바꿈에서 잘린 이름표만 받고 시전할 수 있는 턴에 턴 종료를 7번 골랐다.
PLANNER_EXECUTER_LABEL = "pe6-decision-state"

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
    # executer 가 action 을 고를 때 Jev 에게 보내는 state. `raw` 는 화면의 live object 를 거르지 않고 담되 계획이
    # 부르는 object 를 앞에 두고 6000자에서 뒤를 자른 것, `members` 는 Jev 에게 무엇이 필요한지 물어 거른 것이다
    # (`jev_state.py`).
    jev_state: Literal["raw", "members"] = "raw"


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
