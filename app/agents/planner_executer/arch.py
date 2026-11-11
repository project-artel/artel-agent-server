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
# pe3: STATE_SET 을 읽는 executer, 행동 지침만 내는 planner, 실패를 가르는 evaluator 로 엔진을 다시 짰다. 기본으로 켜지는
# 것은 STATE_SET(`state_source`), 실패 분석(`analysis`), editor(`editor`), 화면 사진(`screen_images`)이다. 행동의 모양
# (`actions`, 기본 `legacy`), 대상 고정(`target_contract`)과 fact(`facts`)는 기본으로 꺼져 있고, 복구는 기본이 editor 다.
# - executer: Jev 가 읽는 state 를 Orchestration `STATE_SET` 에서 읽는다(knob `state_source`, `app/qa/state_set.py`). planner 가
#   step 마다 ```observe 로 Jev 가 볼 범위를 정하고, Jev 는 EXPAND_STATE(범위를 넓힘)·REPLAN(계획을 다시 씀)을 고를 수 있으며
#   직전 action turn 들(`RECENT_ACTIONS_SHOWN`)을 함께 읽는다. 행동은 효과를 본 것만 성공으로 친다. 헛도는 action·같은 action
#   의 반복·입력 없음은 런 단위 장부(`ledger.py`)와 함께 막힘으로 세고, 막히면 editor 가 macro 를 고치거나(`app/agents/editor/`)
#   planner 가 계획 수정(```changes)으로 다시 쓴다(knob `repair`).
# - planner: knob `actions="guidance"` 면 등록 행동(developer macro)을 고르고 행동 지침(```guidance)만 낸다. 준비 조건과 효과는
#   등록 계약(`contract.py`)과 developer macro 앞부분의 `require` 로 executer 가 집행한다. ```check 로 판정에 쓸 값을,
#   ```after 로 꼭 붙어야 하는 행동 순서를 정한다. 쓸 때마다 화면 사진을 본다(knob `screen_images`).
# - 대상: knob `target_contract` 면 step 이 가리키는 대상을 런 시작 때 정하고(`target.py`), 개발자가 선언한 게임 값의
#   뜻(`identities.py`)을 읽는다. 대상이 아닌 것의 통과는 막는다.
# - fact: knob `facts` 면 개발자가 선언한 fact·rule(`facts.py`)을 planner 가 ```facts 로 고르고, Jev 가 그 값을 state 맨 위
#   `facts` 에서 읽는다. guidance 가 고르지 않은 fact 를 부르면 계획을 거절한다.
# - evaluator: 판정을 `app/agents/evaluator/` 로 뗐다. ```check 값, play 전후의 전체 변화, 화면 사진을 읽고 판정과 game_insight
#   를 낸다. 실패 step 은 원인을 가르고(knob `analysis`) 런 리뷰를 쓴다.
# prompt 는 qa_planner v4 를 읽는다.
# pe4는 editor 복구에서 행동 계약을 보존하고 PulseMemory 판정에 시작·끝 관측값을 전달한다.
# pe5: macro 의 `actionable()` 이 SDK 가 보이지 않는다고 보고한 대상(`pulse` 의 `visible` 이 false — 로딩 화면 같은 덮개 뒤,
# 투명, mask 밖)을 거짓으로 읽는다(ARTEL-1436). planner 가 읽는 macro 문법 글의 Waiting 절에 그 말을 더해, 덮개가 걷히기 전에
# 누르지 않도록 `wait_until(actionable(...))` 를 쓰게 한다. `visible` 을 보내지 않는 옛 SDK 에서는 `actionable()` 이 그대로다.
PLANNER_EXECUTER_LABEL = "pe5-actionable-visible"

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
    # 실패한 step 의 분석과 런 끝의 리뷰(ARTEL-1267). `off` 면 둘 다 하지 않고 `STATUS` 에 `analysis` 를
    # 싣지 않는다 — pe2 의 frame 그대로다. 실패 하나에 모델 호출 하나, run 하나에 하나가 더 든다.
    analysis: Literal["off", "on"] = "on"
    # 런 중 macro 수정(ARTEL-1288). `off` 면 editor 를 부르지 않는다.
    editor: Literal["off", "on"] = "on"
    # 헛도는 action 을 누가 고치나. `editor` 는 그 action 하나를 editor 가 고친다. `planner` 는 play 를 멈추고 planner 가
    # 실패 기록과 값의 변화를 읽고 step 을 다시 쓴다(`max_repairs_per_step` 번까지, step 예산은 이어서 센다). `editor="off"` 면
    # 둘 다 하지 않는다.
    repair: Literal["editor", "planner"] = "editor"
    # 한 action 이 이만큼 연달아 실패하거나 입력 없이 끝나면 editor 를 부른다. `max_failures_in_a_row` 와 같은 수면
    # 실패 셋째에 멈춤보다 고침이 먼저다.
    repair_after: int = Field(default=3, ge=1, le=50)
    # step 하나에서 editor 를 부르는 횟수. 거절로 끝난 호출도 센다.
    max_repairs_per_step: int = Field(default=2, ge=0, le=10)
    # action 이 돌았는데 게임에 입력을 하나도 보내지 않은 turn 이 이만큼 이어지면 play 를 멈춘다. 어느 action 이든 센다.
    max_idle_turns_in_a_row: int = Field(default=10, ge=1, le=1_000)
    # 같은 action 을 이만큼 연달아 고르면 진척이 없는 것으로 본다. `repair="planner"` 면 planner 에게 step 을 다시 쓰게 하고,
    # 아니면 play 를 멈춘다. 그 action 이 통과하고 입력도 보내 다른 규칙에 안 걸리는 반복을 잡는다.
    max_same_action_in_a_row: int = Field(default=6, ge=2, le=1_000)
    # planner(step 을 쓰거나 다시 쓸 때)와 evaluator(판정할 때)가 화면 한 장을 그림으로 본다. Jev 에게는 보내지 않는다.
    screen_images: Literal["off", "on"] = "on"
    # 프로젝트의 developer macro 를 런에 싣는다. `off` 면 planner 가 행동 macro 를 전부 직접 쓴다 — developer macro 없이
    # planner 혼자 어디까지 가는지 재는 비교 축이다.
    developer_macros: Literal["off", "on"] = "on"
    # planner 가 쓰는 행동의 모양. `legacy` 는 인자 없는 macro `def`, `contract` 는 ```actions 계약과 바인딩되는
    # body(`contract.py`). developer macro 는 둘 다에서 legacy 로 돈다. 같은 빌드에서 둘을 비교하려고 knob 으로 둔다.
    # `guidance` 는 planner 가 행동을 쓰지 않고 지침과 등록 행동 이름만 낸다.
    actions: Literal["legacy", "contract", "guidance"] = "legacy"
    # step 의 대상 계약(`target.py`). `on` 이면 런 시작 때 대상을 해석해 고정하고 planner·executer·evaluator 가 따른다.
    # ORDERED 시나리오에만 쓰인다. prompt v4 부터 있는 `target_resolver` 를 읽는다. `off` 면 대상을 정하지 않는다.
    target_contract: Literal["off", "on"] = "off"
    # 개발자가 선언한 fact·rule(`facts.py`). `on` 이면 planner 가 읽고 step 마다 쓸 fact 를 고르며, Jev state 에 그 값이 실린다.
    # 선언은 `qa_facts_dir` 에서 읽는다. 선언이 없으면 `off` 와 같다.
    facts: Literal["off", "on"] = "off"
    # Executer 가 Jev 에게 보내는 state 의 출처(ARTEL-1297). `state_set` 은 Orchestration 의 `STATE_SET`,
    # `pulse_memory` 는 Agent 가 합친 `PulseMemory`. 둘을 같은 빌드에서 비교하려고 knob 으로 둔다.
    state_source: Literal["state_set", "pulse_memory"] = "state_set"
    # Jev 가 읽을 범위를 planner 가 정한다(ARTEL-1295 C). `all` 은 live object 전부. `planner` 는 `state_set` 일 때만
    # 뜻이 있다 — `PulseMemory` 출처는 범위를 좁히지 않는다.
    observation: Literal["planner", "all"] = "planner"
    # Jev 가 `EXPAND_STATE` 를 고를 수 있는 횟수(step 마다). 단계가 둘이라 2 를 넘어도 더 넓어지지 않는다.
    max_expansions_per_step: int = Field(default=2, ge=0, le=2)
    # Jev 가 `REPLAN` 을 골라 planner 가 step 을 다시 쓰는 횟수(step 마다). 0 이면 선택지에 없다. 기본은 꺼 둔다.
    max_replans_per_step: int = Field(default=0, ge=0, le=5)
    # 한 action 이 한 step 안에서 실패하거나 입력 없이 끝난 횟수(누적)가 이만큼이면 그 step 의 선택지에서 뺀다. editor 가
    # 고친 action 은 다시 0 부터 센다. 전투의 `end_turn` 처럼 가끔 대기 시간을 넘겼다가 다음에 통과하는 action 이 있어
    # `repair_after` 보다 넉넉히 둔다.
    max_misses_per_action: int = Field(default=5, ge=1, le=100)
    # `observation="planner"` 일 때 executer 가 Jev 에게 보내는 state 의 상한(글자). 크기 때문에 거르지 않으므로 안전장치로만
    # 둔다. 6만 자는 약 2만 7천 token 으로 Jev 1.13 의 32k context 에 선택지와 질문을 남긴다.
    jev_state_chars: int = Field(default=60_000, ge=2_000, le=100_000)


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
