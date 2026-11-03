"""화면을 보는 도구 셋.

`observe_scene` 은 마지막 행위 이후 무엇이 바뀌었는지를, `inspect_object` 는 객체 하나가
들고 있는 값 전부를, `capture_screen` 은 실제 그림을 준다.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.arch import ScreenCaptureMode
from app.agents.qa.tools.state import capture_from_action_result
from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_tool_description, resolve_version, roles_in
from app.qa.envelope import JsonRpcAction, LogCategory
from app.qa.game_errors import render_errors
from app.qa.performance import PerformanceUnavailable, render_summary, summarize

# `get_performance` 가 창을 안 받았을 때 쓰는 창. tool 결과 끝의 한 줄과 같은 10초다.
DEFAULT_PERFORMANCE_SECONDS = 10.0


def _diagnostic_description(name: str, first_version: str) -> str:
    """구버전 prompt에 없는 진단 도구만 최초 지원 description을 사용한다."""
    version = resolve_version("qa_run")
    if f"tool_{name}" not in roles_in("qa_run", version):
        version = first_version
    return load_tool_description(name, version).body


def build_observation_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state = ctx.channel, ctx.state
    _answer = ctx.answer

    @tool(description=load_tool_description("observe_scene").body)
    async def observe_scene(
        step: int, thought: str, wait_seconds: float = 0.0, current_scene: bool = False
    ) -> str:
        arrived = await channel.look(wait_seconds)
        await ctx.performance().refresh_summary()
        messages = channel.drain_operator_messages()
        if not arrived:
            return _answer(
                "The game did not answer. It may be loading, or it may be stuck. "
                "You can look again with a longer wait, or judge the step failed.",
                messages,
            )
        # 관측은 행위가 아니다. 마지막 **행위** 이후로 무엇이 쌓였는지를 그대로 보여준다 —
        # 관측할 때마다 경계를 옮기면, 두 번 보는 것만으로 그 사이의 변화를 잃는다.
        #
        # 그 경계를 `_answer` 가 들고 있으므로 여기서 따로 그리지 않는다. 이 도구는 화면이
        # 곧 답이라 몸통이 비어 있고, 화면은 `_answer` 에서 붙는다.
        if not current_scene:
            return _answer("", messages)

        # 전량은 창을 안 탄다. 그래서 `_answer` 의 창 뷰를 끄고 여기서 그린다 — 둘 다 내면
        # 같은 화면이 두 번 실린다(ARTEL-635 에서 이미 한 번 그랬다).
        #
        # 행위 경계(`state.last_action_frame`)는 **안 건드린다.** 관측은 행위가 아니고,
        # 전량을 봤다고 해서 그 사이 무엇이 쌓였는지를 잊어도 되는 것이 아니다.
        view = channel.scene.current_scene()
        state.watermark = channel.scene.updates
        return _answer(view, messages, screen=False)

    @tool(description=load_tool_description("inspect_object").body)
    async def inspect_object(step: int, selector: str, thought: str) -> str:
        found = channel.scene.pulse.inspect(selector)
        messages = channel.drain_operator_messages()
        return _answer(found, messages)

    @tool(description=_diagnostic_description("get_errors", "v31"))
    async def get_errors(step: int, thought: str, include_seen: bool = False) -> str:
        # What the agent reads is `qa_run/<version>/tool_get_errors.md`.
        # 게임에 아무것도 묻지 않는다. `PULSE` 와 `ACTION_RESULT` 가 이미 실어 온 것을 읽는다.
        # 화면은 안 붙인다 — 화면을 바꾸지 않는 tool 이라 view 를 붙이면 지난 view 를 반복한다.
        ledger = channel.game_errors
        items = ledger.take(include_seen)
        return _answer(
            render_errors(items, ledger, include_seen),
            channel.drain_operator_messages(),
            screen=False,
        )

    @tool(description=_diagnostic_description("get_performance", "v34"))
    async def get_performance(
        step: int,
        thought: str,
        since_frame: int | None = None,
        last_seconds: float | None = None,
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_get_performance.md`.
        # 둘 다 없으면 지난 10초다. 둘 다 오면 orchestration 이 둘을 함께 건다.
        if since_frame is None and last_seconds is None:
            last_seconds = DEFAULT_PERFORMANCE_SECONDS
        window = (
            f"frames since {since_frame}" if since_frame is not None else ""
        ) + (
            f"{' and ' if since_frame is not None else ''}the last {last_seconds:g} s"
            if last_seconds is not None
            else ""
        )
        try:
            samples = await ctx.performance().fetch(since_frame, last_seconds)
        except PerformanceUnavailable as error:
            body = (
                f"Performance data could not be read — {error}. Judge from what you see, "
                "and do not ask again in this step."
            )
        else:
            body = render_summary(summarize(samples), window)
        return _answer(body, channel.drain_operator_messages(), screen=False)

    return [observe_scene, inspect_object, get_errors, get_performance]


# capture 답에 붙는 점검 목록 (ARTEL-1221). 2026-10-22 측정에서 agent 는 capture 를 찍은 목적만
# 답했다 — 카드 그림이 없는 빌드에서 3런 모두 손패를 capture 하고도 그림이 없다는 말이 없었고,
# 개발자 콘솔이 손패를 가린 capture 두 장도 지나쳤다. 그림이 도착하기 직전에 읽히는 자리라
# system prompt 보다 여기에 둔다.
CAPTURE_CHECKLIST = (
    "Answer what you captured it for, then look once more for: empty or black areas where "
    "something should be drawn, elements with no art (a card or button that is only text), "
    "elements overlapping each other, text or images cut off at an edge, and a developer "
    "console or error text on screen. File each one you see with `report_issue`."
)


def build_capture_tool(ctx: ToolContext) -> BaseTool:
    """`capture_screen` 하나만 낸다.

    `arch.vision` 이 켜진 런에만, 그것도 목록 맨 뒤에 붙는다. 그 조건이 조립하는 자리에서
    보여야 해서 다른 관찰 도구와 따로 낸다.
    """
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state, arch = ctx.channel, ctx.state, ctx.arch
    _answer = ctx.answer

    # Both arms of the screen-capture comparison run on one `prompt_version`, so the
    # every-call note is chosen here. `arch_fingerprint` hashes `screen_capture`, so
    # the two descriptions still land in two structures rather than folding into one.
    description = load_tool_description("capture_screen").body.format(
        limit=arch.max_captures_per_run
    )
    if arch.screen_capture is not ScreenCaptureMode.on_demand:
        description += " " + load_tool_description("capture_screen_every_call").body

    @tool(description=description)
    async def capture_screen(step: int, thought: str, target_id: int | None = None) -> str:
        # What the agent reads is tool_capture_screen.md, not this.
        # Returns the capture as a promise: the image itself is handed to the
        # vision middleware and arrives on the next model call.
        if state.captures_attempted >= arch.max_captures_per_run:
            # Refused with the reason, not silently: a run that keeps looking
            # instead of deciding will reach the deadline with nothing reported.
            return (
                f"You have used all {arch.max_captures_per_run} screenshots for this run. "
                "Judge the remaining steps from the scene text."
            )

        state.captures_attempted += 1
        params: list[Any] = [] if target_id is None else [target_id]
        what = "the screen" if target_id is None else f"element {target_id}"
        result = await channel.dispatch_actions(
            [JsonRpcAction(id=1, method="capture_screen", params=params)],
            f"Capturing {what}",
            step,
        )
        messages = channel.drain_operator_messages()

        if result is None or not result.results:
            return _answer(
                "The game did not answer the capture. Try again, or judge from the "
                "scene text.",
                messages,
            )

        item = result.results[0]
        if not item.success:
            # Says what to do instead, not just what went wrong. A game built on an SDK
            # without this action answers "Unsupported method" to every capture, and an
            # agent told only that failed the step and then the whole run — over a
            # screenshot it could have done without.
            return _answer(
                f"The screen could not be captured — {item.error or 'no reason given'}. "
                "Judge this step from the scene text instead, and do not capture again "
                "in this run.",
                messages,
            )

        capture = capture_from_action_result(item, what, whole_screen=target_id is None)
        if capture is None:
            return _answer(
                "The game reported a capture but no image to read. Judge from the "
                "scene text.",
                messages,
            )

        state.add_pending_capture(capture)
        # On the timeline so a reviewer can open exactly what the agent looked at.
        #
        # Not what separates a tool capture from an automatic one, though it looks
        # like it could: a tool capture that fails or times out returns above
        # without writing this note, so subtracting notes from `capture_screen`
        # ACTION rows overcounts the automatic ones. What separates them exactly is
        # `step`. This dispatch always carries one because the tool requires it;
        # `QaCaptureVisionMiddleware` sends none, so its ACTION rows have
        # `payload.step` null (ARTEL-868).
        await channel.note(f"Captured {what}: {capture.url}", LogCategory.OBSERVATION, step)

        return _answer(f"Captured {what}. The image follows. {CAPTURE_CHECKLIST}", messages)

    return capture_screen
