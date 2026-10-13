"""다음 step 에 쓸 수 있는 등록된 macro 를 찾아 `report_step` 의 답에 붙일 문장을 만든다.

macro 목록은 첫 메시지에 한 번 실리고 뒤로 멀어진다. 두세 step 이 지나면 agent 는 목록을
다시 보지 않고 손으로 시작하는데(`macro-continuity-l1` 에서 run 2 가 그랬다), 그 순간 곁에
있는 것은 방금 받은 `report_step` 의 답이다. 그래서 다음 step 번호가 이미 답에 적히는 자리에
그 번호의 macro 이름을 같이 적는다.

모델을 부르지 않는다. 이름과 summary 의 모양만 보는 순수 함수라 같은 입력이면 같은 문장이다.
초안 이름이 `replay_step_N` 이고 첫 주석이 `# Step N: ...` 인 것은 `lift.py` 가 정한 모양이다.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.agents.qa.macro.book import RegisteredMacro
from app.qa.scene_context import SceneMacro

# 한 답에 이름을 대는 macro 수. 셋 이상이면 어느 것을 먼저 돌릴지 고르는 일이 agent 에게 남는다.
MAX_NAMED_MACROS = 2
# summary 를 괄호 안에 옮길 때의 길이. 한 문장에 둘이 들어가도 답이 길어지지 않게 자른다.
MAX_SUMMARY_CHARS = 100


@dataclass(frozen=True)
class StepMacro:
    name: str
    summary: str | None


def first_body_comment(source: str, entry_name: str) -> str | None:
    """`entry_name` 의 `def` 본문 안 첫 `#` 줄에서 `#` 과 공백을 뗀 글. 없으면 `None`.

    orchestration 이 `summary` 를 만드는 `firstBodyComment` 와 같은 규칙이다. `def` 줄보다
    앞의 주석은 본문이 아니므로 보지 않는다.
    """
    in_body = False
    for line in source.splitlines():
        stripped = line.strip()
        if not in_body:
            in_body = stripped.startswith(f"def {entry_name}(")
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("# ").strip() or None
    return None


def _mentions_step(step: int, name: str, summary: str | None) -> bool:
    if summary is not None and summary.lstrip().startswith(f"Step {step}:"):
        return True
    # `Step 1:` 과 `Step 13:` 은 콜론 때문에 갈리고, 이름은 `replay_step_1` 과
    # `replay_step_1_2`(같은 step 의 두 번째 초안)만 같은 step 으로 읽는다.
    return re.fullmatch(rf"replay_step_{step}(_\d+)?", name) is not None


def macros_for_step(
    step: int,
    registrations: Mapping[str, RegisteredMacro],
    scene_macros: Sequence[SceneMacro],
) -> list[StepMacro]:
    """`step` 에 맞는 macro 를 최대 `MAX_NAMED_MACROS` 개. 이 런이 등록한 것이 먼저다.

    이 런의 등록은 방금 고친 것일 수 있어 지난 런의 같은 이름보다 믿을 만하다. 같은 이름은
    한 번만 센다.
    """
    found: list[StepMacro] = []
    for name, registered in registrations.items():
        summary = first_body_comment(registered.definition.source, registered.definition.entry.name)
        if _mentions_step(step, name, summary):
            found.append(StepMacro(name, summary))
    for macro in scene_macros:
        if any(one.name == macro.name for one in found):
            continue
        if _mentions_step(step, macro.name, macro.summary):
            found.append(StepMacro(macro.name, macro.summary))
    return found[:MAX_NAMED_MACROS]


def _described(macro: StepMacro) -> str:
    if not macro.summary:
        return f"`{macro.name}`"
    text = " ".join(macro.summary.split())
    if len(text) > MAX_SUMMARY_CHARS:
        text = text[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return f"`{macro.name}` ({text})"


def render_step_macro_hint(step: int, macros: Sequence[StepMacro]) -> str:
    """답 끝에 붙이는 문장. 맞는 macro 가 없으면 빈 문자열."""
    if not macros:
        return ""
    listed = ", ".join(_described(macro) for macro in macros)
    if len(macros) == 1:
        lead = f"Step {step} has a registered macro: {listed}."
        run = "Run it"
    else:
        lead = f"Step {step} has registered macros: {listed}."
        run = "Run one of them"
    return (
        f"\n\n{lead} {run} with `run_macro` as the first action of step {step}; "
        "act by hand only if it fails or does not cover the step."
    )
