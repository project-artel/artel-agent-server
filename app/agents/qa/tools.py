"""The tools the QA agent drives the game with.

Each one is a round trip the agent chooses to make, which is what separates this
from the old design: the run advances because the agent asked, not because the
game happened to send something.
"""

from typing import Any

from langchain_core.tools import StructuredTool

from app.qa.channel import QaRunChannel, with_operator_messages
from app.qa.envelope import (
    JsonRpcAction,
    LogCategory,
    MessageType,
    RunResult,
    StatusPayload,
    StepStatus,
)
from app.qa.schemas import QaStepResult


class QaRunState:
    """What the loop has done so far, for the tools that need to know."""

    def __init__(self, total_steps: int) -> None:
        self.total_steps = total_steps
        self.step_results: list[QaStepResult] = []
        self.finished = False
        # The observation the agent last saw, so the next look is a diff.
        self.watermark = 0


def build_tools(channel: QaRunChannel, state: QaRunState) -> list[StructuredTool]:
    async def observe_scene(step: int, thought: str, wait_seconds: float = 0.0) -> str:
        """Look at the game screen. Returns what changed since your last look.

        Use `wait_seconds` when the screen needs time first — a loading screen, an
        animation, a countdown. Always look before acting.

        `step` is the scenario step you are working on and `thought` is why you
        are looking; both go on the timeline.
        """
        await channel.note(thought, LogCategory.THOUGHT, step)
        arrived = await channel.look(wait_seconds, thought, step)
        messages = channel.drain_operator_messages()
        if not arrived:
            return with_operator_messages(
                "The game did not answer. It may be loading, or it may be stuck. "
                "You can look again with a longer wait, or judge the step failed.",
                messages,
            )
        view = channel.scene.render(state.watermark)
        state.watermark = channel.scene.updates
        return with_operator_messages(view, messages)

    async def _run(
        actions: list[JsonRpcAction], thought: str, summary: str, step: int
    ) -> str:
        """Every acting tool goes through here: log the reasoning, act, look.

        Takes a list because a drag is only a drag when its actions ride in one
        batch — the SDK runs a batch strictly in order, so nothing can slip
        between the press and the release.
        """
        await channel.note(thought, LogCategory.THOUGHT, step)
        result, looked = await channel.act_and_look(actions, summary, step)
        messages = channel.drain_operator_messages()

        if result is None:
            return with_operator_messages(
                "The game reported no result. It may still have run — observe the "
                "scene to find out what actually happened.",
                messages,
            )

        methods = {action.id: action.method for action in actions}
        lines = []
        for item in result.results:
            # Anything past the batch is the trailing scan_scene, which is ours
            # rather than something the agent asked for.
            if item.id not in methods:
                continue
            outcome = "ok" if item.success else f"FAILED — {item.error or 'no reason given'}"
            # Named, because a drag comes back as four lines and an unlabelled
            # failure would not say which part of it went wrong.
            lines.append(f"  {methods[item.id]}: {outcome}")
        body = "\n".join(lines) or "  (the game returned no outcome for this action)"

        if looked:
            view = channel.scene.render(state.watermark)
            state.watermark = channel.scene.updates
            body = f"{body}\n\n{view}"
        else:
            body = f"{body}\n\nThe scene did not arrive; observe again to see the result."
        return with_operator_messages(body, messages)

    async def click_button(step: int, target_id: int, thought: str) -> str:
        """Click a button. `target_id` must be an id from the scene you just saw.

        `step` is the scenario step this belongs to and `thought` is why you are
        clicking; both go on the timeline.
        """
        return await _run(
            [JsonRpcAction(id=1, method="button_click", params=[target_id])],
            thought,
            f"Clicking {target_id}",
            step,
        )

    async def enter_text(step: int, target_id: int, value: str, thought: str) -> str:
        """Type into a text field. `target_id` must be an id from the current scene."""
        return await _run(
            [JsonRpcAction(id=1, method="enter_text", params=[target_id, value])],
            thought,
            f"Typing into {target_id}",
            step,
        )

    async def press_key(step: int, key_code: str, duration_seconds: float, thought: str) -> str:
        """Press a key — no target needed, so this works on a screen with nothing
        clickable, such as a dialogue or cutscene that advances on any key.

        `key_code` is a Unity KeyCode name, e.g. "Space", "Return", "Escape".
        `duration_seconds` must be greater than zero.
        """
        return await _run(
            [JsonRpcAction(id=1, method="key_click", params=[key_code, duration_seconds])],
            thought,
            f"Pressing {key_code}",
            step,
        )

    async def move_pointer(step: int, x: float, y: float, thought: str) -> str:
        """Move the pointer to a point on the screen, without pressing anything.

        `x` and `y` are screen pixels with the origin at the BOTTOM-LEFT corner,
        so y grows upwards. Use this to hover, or to put the pointer somewhere a
        target id cannot address — a map, a canvas, an inventory slot.
        """
        return await _run(
            [JsonRpcAction(id=1, method="move_mouse", params=[x, y])],
            thought,
            f"Moving the pointer to ({x}, {y})",
            step,
        )

    async def hold_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Press a mouse button and keep it down, at wherever the pointer now is.

        `button` is 0 for left, 1 for right, 2 for middle. The press happens at
        the current pointer position — move there first with `move_pointer`.

        Nothing releases this for you. Call `release_mouse_button` before you
        judge the step, or every later step runs with the button still down. For
        a plain drag, use `drag_pointer` instead — it cannot be left half-done.
        """
        return await _run(
            [JsonRpcAction(id=1, method="mouse_down", params=[button])],
            thought,
            f"Holding mouse button {button}",
            step,
        )

    async def release_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Let go of a mouse button held by `hold_mouse_button`.

        `button` must be the one you pressed: 0 for left, 1 for right, 2 for
        middle. The release lands at wherever the pointer now is, which is what
        decides where a drag drops.
        """
        return await _run(
            [JsonRpcAction(id=1, method="mouse_up", params=[button])],
            thought,
            f"Releasing mouse button {button}",
            step,
        )

    async def hold_key(step: int, key_code: str, thought: str) -> str:
        """Press a key and keep it down until you release it.

        `key_code` is a Unity KeyCode name, e.g. "W", "LeftShift", "Space". Use
        this for movement and modifiers — anything the game reads as "is it held
        right now" rather than "was it pressed". `press_key` is the one-shot.

        Nothing releases this for you. Call `release_key` before you judge the
        step, or the game keeps seeing the key down for the rest of the run.
        """
        return await _run(
            [JsonRpcAction(id=1, method="key_down", params=[key_code])],
            thought,
            f"Holding {key_code}",
            step,
        )

    async def release_key(step: int, key_code: str, thought: str) -> str:
        """Let go of a key held by `hold_key`. `key_code` must be the same one."""
        return await _run(
            [JsonRpcAction(id=1, method="key_up", params=[key_code])],
            thought,
            f"Releasing {key_code}",
            step,
        )

    async def drag_pointer(
        step: int,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        thought: str,
        button: int = 0,
    ) -> str:
        """Drag from one point on the screen to another and drop there.

        Coordinates are screen pixels with the origin at the BOTTOM-LEFT corner.
        `button` is 0 for left, 1 for right, 2 for middle.

        Prefer this over pressing and releasing yourself: the press, the move and
        the release go to the game as ONE batch, which the game runs strictly in
        order, so the drag cannot be interrupted or left with the button down.
        """
        return await _run(
            [
                # The press takes no coordinates — it lands wherever the pointer
                # already is, so the drag has to start by moving there.
                JsonRpcAction(id=1, method="move_mouse", params=[from_x, from_y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="move_mouse", params=[to_x, to_y]),
                JsonRpcAction(id=4, method="mouse_up", params=[button]),
            ],
            thought,
            f"Dragging from ({from_x}, {from_y}) to ({to_x}, {to_y})",
            step,
        )

    async def report_step(step: int, passed: bool, message: str, thought: str) -> str:
        """Record the verdict for one scenario step, with the evidence for it.

        Call this once per step, right after you have observed the result of that
        step's action. `message` should cite what you saw, and `thought` is how
        you reached the verdict — it goes on the timeline beside it.
        """
        await channel.note(thought, LogCategory.THOUGHT, step)
        state.step_results.append(QaStepResult(step=step, passed=passed, message=message))
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED if passed else StepStatus.FAILED,
                step=step,
                message=message,
            ),
        )
        remaining = state.total_steps - len(state.step_results)
        if remaining > 0:
            return f"Recorded. {remaining} step(s) left."
        return "Recorded. That was the last step — finish the run."

    async def finish_run(passed: bool, summary: str, thought: str) -> str:
        """End the run. Call this once, after the last step has been reported.

        `thought` is how you reached the overall verdict; it goes on the timeline.
        """
        await channel.note(thought, LogCategory.THOUGHT)
        total = state.total_steps
        passed_count = sum(1 for result in state.step_results if result.passed)
        state.finished = True
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED,
                result=RunResult.PASSED if passed else RunResult.FAILED,
                message=summary,
                summary={
                    "total": total,
                    "passed": passed_count,
                    "failed": total - passed_count,
                    "steps": [result.model_dump() for result in state.step_results],
                },
            ),
        )
        return "The run is closed."

    async def reply_to_operator(message: str, thought: str, step: int | None = None) -> str:
        """Answer the operator. Use when they asked something, not for progress.

        `thought` is why you are answering this way; it goes on the timeline so a
        reviewer can see the reasoning behind what the operator was told.
        """
        await channel.note(thought, LogCategory.THOUGHT, step)
        await channel.say(message, step)
        return "Sent."

    return [
        StructuredTool.from_function(coroutine=observe_scene, name="observe_scene"),
        StructuredTool.from_function(coroutine=click_button, name="click_button"),
        StructuredTool.from_function(coroutine=enter_text, name="enter_text"),
        StructuredTool.from_function(coroutine=press_key, name="press_key"),
        StructuredTool.from_function(coroutine=move_pointer, name="move_pointer"),
        StructuredTool.from_function(coroutine=hold_mouse_button, name="hold_mouse_button"),
        StructuredTool.from_function(
            coroutine=release_mouse_button, name="release_mouse_button"
        ),
        StructuredTool.from_function(coroutine=hold_key, name="hold_key"),
        StructuredTool.from_function(coroutine=release_key, name="release_key"),
        StructuredTool.from_function(coroutine=drag_pointer, name="drag_pointer"),
        StructuredTool.from_function(coroutine=report_step, name="report_step"),
        StructuredTool.from_function(coroutine=finish_run, name="finish_run"),
        StructuredTool.from_function(coroutine=reply_to_operator, name="reply_to_operator"),
    ]
