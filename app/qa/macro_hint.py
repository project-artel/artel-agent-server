# -*- coding: utf-8 -*-
"""손으로 움직이는 step 에 맞는 등록된 macro 한 줄을 Jev 가 고르게 한다 (ARTEL-1026).

agent 는 첫 메시지의 macro 목록을 두세 step 뒤에는 다시 보지 않고 손으로 시작한다. 한 step 에서
세 번째 손 조작이 나갈 때 그 step 의 문장과 지금 서 있는 scene 에서 쓸 수 있는 macro 를 Jev 에 물어,
맞는 것이 있으면 그 조작의 결과에 한 줄로 알린다(`action_tools._recorded`).

후보는 이 빌드에 등록된 macro 중 지금 scene 을 단 것이다. 이 런이 등록한 macro 는 `screen.id` 만
알고 scene 이름을 모르므로(`for_step.macros_for_step` 과 같다) scene 이름이 하나도 없는 것은 후보에
든다. 목록과 줄 모양은 `for_step` 과 `scene_context` 의 것을 읽기만 한다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

from app.agents.qa.macro.book import RegisteredMacro
from app.agents.qa.macro.for_step import StepMacro
from app.qa import jev
from app.qa.scene_context import SceneMacro, scene_labels

logger = logging.getLogger(__name__)

# Jev 에게 보이는 macro 수. 선택지가 늘면 고르는 일이 흐려지고 요청도 커진다.
MAX_OPTIONS = 8
NONE_OPTION = "none"

Choose = Callable[..., Awaitable[str | None]]


def first_line(docstring: str | None) -> str:
    """docstring 의 첫 줄. 끝의 마침표는 뗀다(hint 문장이 마침표로 이어진다)."""
    if not docstring or not docstring.strip():
        return ""
    return docstring.strip().splitlines()[0].strip().rstrip(".")


def _on_scene(macro: StepMacro, scene: str) -> bool:
    if scene in macro.scenes:
        return True
    # 이 런의 등록은 `screen <id>` 라벨만 있다. 그 라벨뿐이면 scene 을 모르는 것이지 다른 scene
    # 이라고 아는 것이 아니다.
    return bool(macro.scenes) and all(label.startswith("screen ") for label in macro.scenes)


def candidates(
    scene: str,
    registrations: Mapping[str, RegisteredMacro],
    scene_macros: Sequence[SceneMacro],
) -> list[StepMacro]:
    """`scene` 에서 쓸 수 있는 등록된 macro, 최대 `MAX_OPTIONS` 개. 이 런의 등록이 먼저다."""
    scene = scene.strip()
    if not scene:
        return []
    found: list[StepMacro] = []
    for name, registered in registrations.items():
        entry = registered.definition.entry
        found.append(
            StepMacro(
                name,
                entry.docstring,
                tuple((one.name, one.declared_type.value) for one in entry.parameters),
                tuple(f"screen {screen_id}" for screen_id in registered.screens),
            )
        )
    for macro in scene_macros:
        if any(one.name == macro.name for one in found):
            continue
        found.append(
            StepMacro(
                macro.name,
                macro.summary,
                tuple((one.name, one.type) for one in macro.parameters),
                tuple(scene_labels(macro.screens)),
            )
        )
    fitting = [one for one in found if one.name != NONE_OPTION and _on_scene(one, scene)]
    return fitting[:MAX_OPTIONS]


def render_line(macro: StepMacro) -> str:
    params = ", ".join(f"{name}: {kind}" if kind else name for name, kind in macro.parameters)
    summary = first_line(macro.summary)
    described = f" — {summary}" if summary else ""
    return (
        f"A registered macro fits this step: {macro.name}({params}){described}. "
        "Call run_macro instead of acting by hand."
    )


async def hint_line(
    step_text: str,
    scene: str,
    registrations: Mapping[str, RegisteredMacro],
    scene_macros: Sequence[SceneMacro],
    *,
    choose: Choose = jev.choose,
) -> str | None:
    """hint 한 줄. 후보가 없거나 step 문장이 비었거나 Jev 가 `none` 이거나 실패하면 `None`."""
    text = step_text.strip()
    if not text:
        return None
    options = candidates(scene, registrations, scene_macros)
    if not options:
        return None
    instruction = (
        "A QA agent is carrying out this test step in a game. Pick the registered macro that "
        f"performs the whole step, or {NONE_OPTION} if no macro does.\nStep: {text}"
    )
    descriptions = {one.name: first_line(one.summary) or one.name for one in options}
    descriptions[NONE_OPTION] = "No listed macro performs this step."
    picked = await choose(
        instruction, [*(one.name for one in options), NONE_OPTION], descriptions=descriptions
    )
    for one in options:
        if one.name == picked:
            return render_line(one)
    return None


def hint_counts(state) -> tuple[int, int]:
    """(hint 가 실린 step 수, 그중 `run_macro` 를 부른 step 수)."""
    shown = [step for step, was_shown in state.macro_hint_steps.items() if was_shown]
    followed = [step for step in shown if state.macro_calls.calls_in_step(step)]
    return len(shown), len(followed)


def log_hint_counts(state) -> None:
    """hint 가 실릴 때마다 지금까지의 수를 남긴다. 따른 것은 `run_macro` 가 나중에 불려도 센다."""
    shown, followed = hint_counts(state)
    logger.info(
        "[QA] macro hint shown=%d followed=%d asked=%d",
        shown,
        followed,
        len(state.macro_hint_steps),
    )
