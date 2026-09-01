"""화면을 보는 도구 셋.

`observe_scene` 은 마지막 행위 이후 무엇이 바뀌었는지를, `inspect_object` 는 객체 하나가
들고 있는 값 전부를, `capture_screen` 은 실제 그림을 준다.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.state import PendingCapture
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.envelope import JsonRpcAction, LogCategory


# The one description written out here rather than left as a docstring, because
# it has to name the cap and a docstring cannot interpolate one. An agent told
# only "screenshots are limited" spends them at the first opportunity; the number
# is what makes the budget something it can actually ration.
CAPTURE_SCREEN_DESCRIPTION = """Look at the actual picture on screen, not just the scene text.

Use this when the step is about how something *looks*: a layout that may be
broken, a button that may be covered by other UI, a sprite in the wrong state,
text that may be unreadable. The scene listing cannot express any of that.

Leave `target_id` out for the whole screen. Give one to see just that element's
area, at higher detail.

You get {limit} screenshots for the whole run and no more, so spend them on the
steps where looking is what decides the verdict.

The picture arrives right after this result, as its own message."""


def build_observation_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state = ctx.channel, ctx.state
    _answer = ctx.answer

    @tool
    async def observe_scene(
        step: int, thought: str, wait_seconds: float = 0.0, current_scene: bool = False
    ) -> str:
        """Look at the game screen. Returns what changed since your last look.

        Use `wait_seconds` when the screen needs time first — a loading screen, an
        animation, a countdown. Always look before acting.

        `current_scene` asks for the whole scene instead: every object being held
        and every value known, with `(changed)` on the ones that moved since your
        last look. Reach for it when the ordinary view cannot answer you — when a
        value you need has not moved since it was last shown, or when you want to
        act on something the view has not been printing and so have no address
        for. It is several times the size of the ordinary view, so ask for it
        when you need it, not every turn.

        `step` is the scenario step you are working on and `thought` is why you
        are looking; both go on the timeline.
        """
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

    @tool
    async def inspect_object(step: int, selector: str, thought: str) -> str:
        """Read every value the game holds on one object.

        The scene block shows what CHANGED and what you can ACT ON. It does not
        list every value, because most of them do not move and a screen with a
        hundred objects would push everything else out of your context.

        Use this when a step turns on a value you cannot see there — an enemy's
        health, a counter, a flag. `selector` is the address printed in the scene
        block, and a partial one matches (`RangedCat` finds `RangedCat(Clone)[17]`).
        """
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

    @tool(description=CAPTURE_SCREEN_DESCRIPTION.format(limit=arch.max_captures_per_run))
    async def capture_screen(step: int, thought: str, target_id: int | None = None) -> str:
        # What the agent reads is CAPTURE_SCREEN_DESCRIPTION above, not this.
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

        captured = item.returnValue or {}
        url = captured.get("url")
        if not url:
            return _answer(
                "The game reported a capture but no image to read. Judge from the "
                "scene text.",
                messages,
            )

        caption = f"This is {what} right now."
        if captured.get("clipped"):
            # Worth saying out loud: a cropped-off element is itself a finding, and
            # the agent would otherwise read the partial image as the whole thing.
            caption += " Part of it is off the edge of the screen."

        state.add_pending_capture(
            PendingCapture(
                capture_id=str(captured.get("captureId") or ""),
                url=url,
                mime_type=str(captured.get("mimeType") or "image/jpeg"),
                caption=caption,
            )
        )
        # On the timeline so a reviewer can open exactly what the agent looked at.
        await channel.note(f"Captured {what}: {url}", LogCategory.OBSERVATION, step)

        return _answer(f"Captured {what}. The image follows.", messages)

    return capture_screen
