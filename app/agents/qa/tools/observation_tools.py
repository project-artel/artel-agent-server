"""화면을 보는 도구 셋.

`observe_scene` 은 마지막 행위 이후 무엇이 바뀌었는지를, `inspect_object` 는 객체 하나가
들고 있는 값 전부를, `capture_screen` 은 실제 그림을 준다.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.arch import ScreenCaptureMode
from app.agents.qa.tools.state import capture_from_action_result
from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_tool_description
from app.qa.envelope import JsonRpcAction, LogCategory


def build_observation_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state = ctx.channel, ctx.state
    _answer = ctx.answer

    @tool(description=load_tool_description("observe_scene").body)
    async def observe_scene(
        step: int, thought: str, wait_seconds: float = 0.0, current_scene: bool = False
    ) -> str:
        arrived = await channel.look(wait_seconds)
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

    return [observe_scene, inspect_object]


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

        return _answer(f"Captured {what}. The image follows.", messages)

    return capture_screen
