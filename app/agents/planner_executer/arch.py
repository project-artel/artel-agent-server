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
# pe3: 판정을 `app/agents/evaluator/` 로 떼고 실패 분석과 런 리뷰를 더했다(ARTEL-1267). 실패로 판정된 step 마다
# 모델이 원인 분류(`GAME_DEFECT`·`UNREACHED`·`INCONCLUSIVE`·`AGENT_ERROR`)·원인·재현 절차·인사이트를 쓰고, 그것이
# step `STATUS` 의 `analysis` 로 나간다. 재현 절차는 모델이 런의 언어로 쓰되 입력 번호에 묶이고, 코드가 보낸 입력마다
# 사람이 본 값(씬·화면 글자·키)을 소스로 주어 문장이 그 글자를 담는지 검사한다. `ISSUE` 는 보내지 않는다 — 이슈로
# 옮길지는 사람이 판정을 승인하며 정한다. 엔진이 스스로 멈춘 step
# (`PLANNER-REFUSED`·`JEV-ERROR`·`EVALUATOR-ERROR`)과 FREE 의 UNREACHED 는 모델 없이 코드가 채운다. 정상 종료한
# run 은 종단 `STATUS` 에 런 리뷰(`overview`·`patterns`·`insights`)를 싣고 같은 overview 를 `LOG` 한 줄로 낸다.
# 판정 prompt 는 그대로다. 모델이 읽는 새 글 둘(`failure_analysis`·`run_review`, prompt v4)과 knob `analysis`
# 하나가 늘어 fingerprint 도 움직인다. `analysis="off"` 는 pe2 의 frame 과 같다.
#
# pe4: planner 와 evaluator 가 읽는 화면(`prompts.screen`, 곧 `PulseMemory.render`)에 게임이 한 호출이 실린다
# (ARTEL-1285). schema 3 의 `PULSE.events` — `audio.play`·`anim.trigger`·`scene.load`·`spawn`·`prefs.set` 같은
# 호출을 대상·이름·인자·횟수와 함께 — 가 `what happened since the last reading:` 절로 그려진다. 소리가 났는지,
# 애니메이션이 돌았는지는 member 값으로는 안 보이던 것이라 planner 의 `done` 과 evaluator 의 판정이 읽는 근거가
# 늘었다. schema 2 의 reading 은 그 절이 없어 pe3 과 같은 화면이다. knob 과 macro tool 은 그대로라 fingerprint
# 는 움직이지 않고, 모델이 읽는 글이 바뀌어 label 만 움직인다. 같은 변경의 `read_member` 보강(`static()`·
# `member()` 가 `pulse` 에 없는 이름을 게임에 한 번 묻는다)은 macro 가 읽을 수 있는 값을 늘리지만 모델이 읽는
# 글은 아니라 bump 의 이유가 아니다.
#
# pe5: 런 중에 macro 를 고친다(ARTEL-1288). 한 action 이 `repair_after` 번 연달아 실패하거나, 돌았는데 게임에 입력을
# 하나도 보내지 않으면, executer 가 step 을 끝내지 않고 editor(`app/agents/editor/`, prompt `macro_repair`)를 불러 그
# step 의 source 를 다시 쓰게 하고 그 답으로 play 를 잇는다. stage 런 148 은 Jev 가 같은 action 을 37 번 골랐는데 그
# action 이 입력을 한 번도 안 보내 480 초 budget 을 다 썼다 — 발행 전에 macro 를 검증하는 것은 효과를 미리 알 수 없어
# 불가능하고(게임이 답해야 안다), 그 자리에 planner 를 다시 부르면 "step 을 쓰는 역할" 과 "고치는 역할" 이 한 prompt 에
# 섞인다. 그래서 역할을 따로 둔다. 고침은 step 마다 `max_repairs_per_step` 번까지고, 다 쓰면 pe4 의 멈춤 규칙이 그대로
# 적용된다. 입력 없이 도는 turn 이 `max_idle_turns_in_a_row` 번 이어지면 play 를 멈추는 규칙도 새로 든다 — 전에는 turn
# 상한이나 budget 까지 돌았다. knob 넷(`editor`·`repair_after`·`max_repairs_per_step`·`max_idle_turns_in_a_row`)과
# 모델이 읽는 새 글 하나(prompt v5 의 `macro_repair`)가 늘어 fingerprint 와 label 이 함께 움직인다. `editor="off"` 는
# 고치지 않고 멈춤 규칙만 더한 frame 이다. FREE 시나리오의 case play 는 씬 계획 하나를 case 여럿이 나누어 쓰므로 아직
# 고치지 않는다.
# pe6: editor에 선택된 developer macro의 실제 호출과 import 본문, 실행 flags를 함께 전달한다.
# pe5는 planner의 source만 읽어 developer macro를 추측으로 고쳤고, 정상적인 조건 미충족을 알리는
# flag도 입력 없는 실행으로만 보았다(ARTEL-1288). prompt v5와 knob은 유지하지만 모델이 읽는
# evidence가 달라져 label을 구분한다. fingerprint가 해시하는 knob과 tool 목록은 그대로다.
#
# pe7: planner·evaluator·editor 가 읽는 화면(`prompts.screen` = `PulseMemory.render`)이 SDK 가 보내던 것을 전부
# 싣는다(ARTEL-1289) — 컬렉션의 `count`, 못 읽은 멤버의 `(unread: …)`, holder 별 static, 객체의 `ui`·`engine`·`uitk`
# 조각, `coverage gaps`·`audio playing`, `audio.started`·`error` event. 같은 변경의 reader 수정(이름 해석기 하나,
# 모호한 이름 거절, holder 별 `read_member`, 입력 뒤 cache 비움)은 macro 가 읽는 값을 바꾸지 모델이 읽는 글은
# 아니다. knob 은 그대로라 fingerprint 는 움직이지 않고 label 만 움직인다.
#
# pe8: Jev 가 action 을 고를 때 읽는 state 에 직전 action turn 들(`RECENT_ACTIONS_SHOWN`, 3)이 든다(ARTEL-1288).
# 런 153 에서 Jev 는 `prepare_any_spell` 을 11 번 연달아 골랐다 — 조합창이 이미 채워져 macro 가 입력을 하나도
# 보내지 않았는데, Jev 가 받는 state 는 씬과 live object 만이라 화면이 안 움직인 turn 은 첫 turn 과 같아 보였다.
# `recent_actions` 는 "turn 11: prepare_any_spell → passed, sent no input to the game" 같은 줄이고, 실패 사유와
# flag 가 붙는다. 첫 turn 에는 없어 그때의 state 는 pe7 과 같다. 결정 model 이 읽는 글이 바뀌어 label 만 움직인다.
#
# pe9: Executer 가 Jev 에게 보내는 state(`decide`·`ask` 의 기본 state)를 Orchestration 의 `STATE_SET` 에서 읽는다
# (ARTEL-1297). 전에는 Agent 의 `PulseMemory` 가 합친 live object 였다. 객체 모양과 상한은 같고 출처만 바뀐다 —
# 마지막 whole 에 다시 안 실린 객체·멤버(`STALE`)는 빠지고, 동기화 전에는 Jev 를 부르지 않는다. knob
# `state_source` 하나가 늘어 fingerprint 가 움직인다. `state_source="pulse_memory"` 는 pe8 의 frame 이다. planner·
# evaluator·editor 가 읽는 화면(`prompts.screen`)과 macro 의 읽기는 그대로 `PulseMemory` 다.
PLANNER_EXECUTER_LABEL = "pe9-jev-state-set"

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
    # 한 action 이 이만큼 연달아 실패하거나 입력 없이 끝나면 editor 를 부른다. `max_failures_in_a_row` 와 같은 수면
    # 실패 셋째에 멈춤보다 고침이 먼저다.
    repair_after: int = Field(default=3, ge=1, le=50)
    # step 하나에서 editor 를 부르는 횟수. 거절로 끝난 호출도 센다.
    max_repairs_per_step: int = Field(default=2, ge=0, le=10)
    # action 이 돌았는데 게임에 입력을 하나도 보내지 않은 turn 이 이만큼 이어지면 play 를 멈춘다. 어느 action 이든 센다.
    max_idle_turns_in_a_row: int = Field(default=10, ge=1, le=1_000)
    # Executer 가 Jev 에게 보내는 state 의 출처(ARTEL-1297). `state_set` 은 Orchestration 의 `STATE_SET`,
    # `pulse_memory` 는 Agent 가 합친 `PulseMemory`(pe8). 둘을 같은 빌드에서 비교하려고 knob 으로 둔다.
    state_source: Literal["state_set", "pulse_memory"] = "state_set"


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
