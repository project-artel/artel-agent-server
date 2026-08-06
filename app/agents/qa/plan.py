"""cases[] → 실행 계획 전개 (ARTEL-261).

Orche가 보낸 `scenario.cases[]`(TC 내용 + position별 저작 Step)를 Agent가 실행할 스텝
목록으로 전개한다. **판정 단위는 case(=position)**: 각 case는 `expected` 하나에 대해 한 번
판정된다. case 안의 저작 Step(setup/guide/verify)은 그 판정을 어떻게 도달·수행·검증할지에
대한 **어드바이저리 가이드**다 — 실행 시 씬이 다르면 Agent가 무시하고 자기 판단으로 간다.

`cases`가 없으면(구 저작) 레거시 `steps` 경로로 폴백한다 — 이때 산출되는 first_message는
예전 러너의 것과 **바이트 단위로 동일**해서, 프롬프트 v1~v7로 고정한 기존 실행이 흔들리지
않는다.

한 case를 스텝 한 칸으로 편다:

- `reach_first`: 사전조건 + `kind=setup` 스텝의 의도. 판정하지 않고 상태에만 도달한다
  (fast-forward). 못 하면 SETUP-FAILED — 게임 결함이 아니라 도달 불가라, 이슈로 올리지 않는다.
- `do`: `kind=guide`(및 미지 kind) 스텝. 실행 가이드.
- `verify`: `kind=verify` 스텝. 무엇을 볼지.
- `expected`: 그 case의 판정 기준(report_step은 이걸 근거로 한다).

setup/guide/verify와 SETUP-FAILED·advisory의 **의미**는 시스템 프롬프트 v8이 지고, 여기서는
그 의미가 붙을 **데이터**만 만든다.
"""

import json
from dataclasses import dataclass

from app.agents.scenario import ScenarioDraft
from app.qa.cases import QaCase, QaScenarioBody


@dataclass(frozen=True)
class ExecutionPlan:
    """러너가 한 시나리오를 돌리는 데 필요한 두 가지: 첫 메시지와 판정할 스텝 수."""

    first_message: str
    total_steps: int
    uses_cases: bool


def build_execution_plan(body: QaScenarioBody) -> ExecutionPlan:
    """시나리오 본문을 실행 계획으로 전개한다. cases 우선, 없으면 레거시 steps.

    `cases`는 getattr로 본다 — 러너에 (cases 없는) ScenarioDraft가 그대로 들어와도
    레거시 경로로 떨어지게 하려는 방어다.
    """
    if getattr(body, "cases", None):
        return _cases_plan(body)
    return _legacy_plan(body)


# --- cases 경로 (ARTEL-254/258/261) ------------------------------------------


def _render_step_hint(step) -> str:
    """저작 Step 하나를 한 줄 가이드로. 의도가 본문, hint/input은 근거로 덧붙인다."""
    line = step.intent.strip() or "(no intent given)"
    extras: list[str] = []
    if step.hint:
        # 키/조작 힌트는 강제가 아니라 시도해 볼 근거다 — 프롬프트 v8이 그렇게 읽으라 한다.
        extras.append(f"try: {step.hint.strip()}")
    if step.input:
        extras.append(f"via: {step.input.strip()}")
    if step.observe:
        extras.append(f"look at: {step.observe.strip()}")
    if extras:
        line = f"{line}  ({'; '.join(extras)})"
    return line


def _case_to_step(case: QaCase, ordinal: int) -> dict:
    """case 한 칸 → 스텝 dict. 빈 칸은 넣지 않아 모델이 읽을 것만 남긴다."""
    reach: list[str] = []
    if case.precondition and case.precondition.strip():
        reach.append(case.precondition.strip())
    do: list[str] = []
    verify: list[str] = []
    for step in case.steps:
        kind = (step.kind or "guide").strip().lower()
        if kind == "setup":
            reach.append(_render_step_hint(step))
        elif kind == "verify":
            verify.append(_render_step_hint(step))
        else:
            # guide, 그리고 모르는 kind는 실행으로 취급(forward-compat).
            do.append(_render_step_hint(step))

    rendered: dict = {"step": ordinal, "title": case.title or f"Case {ordinal}"}
    if case.category:
        rendered["category"] = case.category
    if reach:
        rendered["reach_first"] = reach
    if do:
        rendered["do"] = do
    if verify:
        rendered["verify"] = verify
    rendered["expected"] = case.expected
    return rendered


def _cases_plan(body: QaScenarioBody) -> ExecutionPlan:
    steps = [_case_to_step(case, index + 1) for index, case in enumerate(body.cases)]
    message = (
        f"Scenario: {body.title} — {body.description}\n\n"
        "Each test case below is one step you judge with `report_step`, against its "
        "`expected`. `reach_first` is the state to get to before you judge — reach it "
        "without a verdict; if you cannot, the case is SETUP-FAILED (see the system "
        "prompt). `do` and `verify` and any hint are the author's guidance — advisory, "
        "not a script.\n\n"
        f"Cases to run in order:\n{json.dumps(steps, ensure_ascii=False, indent=2)}\n\n"
        "Begin. Observe the screen first."
    )
    return ExecutionPlan(first_message=message, total_steps=len(steps), uses_cases=True)


# --- 레거시 경로 (구 저작 steps, 바이트 동일 유지) ---------------------------


def _legacy_plan(body: QaScenarioBody) -> ExecutionPlan:
    """cases 없는 시나리오. 예전 러너 `_first_message`와 동일한 문자열을 만든다.

    본문은 `steps`(ScenarioStep)만 읽는다 — QaScenarioBody가 그 필드를 그대로 들고 있으므로
    ScenarioDraft로 감싸 예전 렌더링을 재현한다.
    """
    scenario = ScenarioDraft(title=body.title, description=body.description, steps=body.steps)
    steps = [
        {
            "step": step.step,
            "title": step.title,
            "state": step.state,
            "action": step.action,
            "expected": step.expected,
        }
        for step in scenario.steps
    ]
    message = (
        f"Scenario: {scenario.title} — {scenario.description}\n\n"
        f"Steps to execute in order:\n{json.dumps(steps, ensure_ascii=False, indent=2)}\n\n"
        "Begin. Observe the screen first."
    )
    return ExecutionPlan(
        first_message=message, total_steps=len(scenario.steps), uses_cases=False
    )
