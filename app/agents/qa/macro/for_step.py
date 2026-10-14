"""다음 step 에 쓸 수 있는 등록된 macro 를 찾아 `report_step` 의 답에 붙일 문장을 만든다.

macro 목록은 첫 메시지에 한 번 실리고 뒤로 멀어진다. 두세 step 이 지나면 agent 는 목록을
다시 보지 않고 손으로 시작하는데(`macro-continuity-l1` 에서 run 2 가 그랬다), 그 순간 곁에
있는 것은 방금 받은 `report_step` 의 답이다. 그래서 다음 step 번호가 이미 답에 적히는 자리에
그 번호의 macro 이름을 같이 적는다.

모델을 부르지 않는다. 이름과 summary 의 모양만 보는 순수 함수라 같은 입력이면 같은 문장이다.
summary 는 진입점 `def` 의 docstring 이다 (ARTEL-984). 초안 이름이 `replay_step_N` 이고
docstring 이 `Step N: ...` 으로 시작하는 것은 `lift.py` 가 정한 모양이다.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.agents.qa.macro.book import RegisteredMacro
from app.qa.scene_context import SceneMacro, render_macro, scene_labels

# 한 답에 이름을 대는 macro 수. 셋 이상이면 어느 것을 먼저 돌릴지 고르는 일이 agent 에게 남는다.
MAX_NAMED_MACROS = 2
# hint 에 옮기는 docstring 의 길이. macro 둘이 실려도 답이 길어지지 않게 자른다.
MAX_SUMMARY_CHARS = 100


@dataclass(frozen=True)
class StepMacro:
    name: str
    # 진입점 `def` 의 docstring. 이 런의 등록은 `MacroFunction.docstring`, 지난 런의 것은
    # orchestration 이 docstring 으로 채운 `SceneMacro.summary` 다.
    summary: str | None
    parameters: tuple[tuple[str, str], ...] = ()
    scenes: tuple[str, ...] = ()


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
        entry = registered.definition.entry
        if _mentions_step(step, name, entry.docstring):
            found.append(
                StepMacro(
                    name,
                    entry.docstring,
                    tuple(
                        (parameter.name, parameter.declared_type.value)
                        for parameter in entry.parameters
                    ),
                    # 이 런의 등록은 `screen.id` 만 안다. scene 이름은 orchestration 이
                    # scene context 에 실어 줄 때 붙는다.
                    tuple(f"screen {screen_id}" for screen_id in registered.screens),
                )
            )
    for macro in scene_macros:
        if any(one.name == macro.name for one in found):
            continue
        if _mentions_step(step, macro.name, macro.summary):
            found.append(
                StepMacro(
                    macro.name,
                    macro.summary,
                    tuple((parameter.name, parameter.type) for parameter in macro.parameters),
                    tuple(scene_labels(macro.screens)),
                )
            )
    return found[:MAX_NAMED_MACROS]


def _described(macro: StepMacro) -> list[str]:
    return render_macro(
        macro.name,
        macro.parameters,
        macro.summary,
        macro.scenes,
        indent="  ",
        docstring_chars=MAX_SUMMARY_CHARS,
    )


def render_step_macro_hint(step: int, macros: Sequence[StepMacro]) -> str:
    """답 끝에 붙이는 문장. 맞는 macro 가 없으면 빈 문자열."""
    if not macros:
        return ""
    listed = [line for macro in macros for line in _described(macro)]
    if len(macros) == 1:
        lead = f"Step {step} has a registered macro:"
        run = "Run it"
    else:
        lead = f"Step {step} has registered macros:"
        run = "Run one of them"
    return (
        f"\n\n{lead}\n" + "\n".join(listed) + f"\n{run} with `run_macro` as the first "
        f"action of step {step}; act by hand only if it fails or does not cover the step."
    )
