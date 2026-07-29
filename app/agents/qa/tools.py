"""The tools the QA agent drives the game with.

Each one is a round trip the agent chooses to make, which is what separates this
from the old design: the run advances because the agent asked, not because the
game happened to send something.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.tools import StructuredTool

from app.agents.qa.vision import MAX_CAPTURES_PER_RUN
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


@dataclass(frozen=True)
class PendingCapture:
    """A captured screen waiting to be put in front of the model."""

    capture_id: str
    url: str
    mime_type: str
    caption: str


class QaRunState:
    """What the loop has done so far, for the tools that need to know."""

    def __init__(self, total_steps: int) -> None:
        self.total_steps = total_steps
        self.step_results: list[QaStepResult] = []
        self.finished = False
        # The observation the agent last saw, so the next look is a diff.
        self.watermark = 0
        # Attempts, not successes. A game whose SDK does not know the capture action
        # refuses every one of them, and counting only what worked would leave that
        # loop unbounded — the cap has to bind on the failing case too.
        self.captures_attempted = 0
        # Handed to the vision middleware on the next model call. The tool cannot
        # return the image itself — an image block on a tool result is rejected by
        # the chat/completions API every model here is reached through.
        self._pending_captures: list[PendingCapture] = []

    def add_pending_capture(self, capture: PendingCapture) -> None:
        self._pending_captures.append(capture)

    def take_pending_captures(self) -> list[PendingCapture]:
        pending = self._pending_captures
        self._pending_captures = []
        return pending


def build_tools(
    channel: QaRunChannel, state: QaRunState, supports_vision: bool = True
) -> list[StructuredTool]:
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

    async def _run(action: JsonRpcAction, thought: str, summary: str, step: int) -> str:
        """Every acting tool goes through here: log the reasoning, act, look."""
        await channel.note(thought, LogCategory.THOUGHT, step)
        result, looked = await channel.act_and_look([action], summary, step)
        messages = channel.drain_operator_messages()

        if result is None:
            return with_operator_messages(
                "The game reported no result. It may still have run — observe the "
                "scene to find out what actually happened.",
                messages,
            )

        lines = []
        for item in result.results:
            # The trailing scan_scene is ours, not something the agent asked for.
            if item.id > 1:
                continue
            outcome = "ok" if item.success else f"FAILED — {item.error or 'no reason given'}"
            lines.append(f"  {outcome}")
        body = "\n".join(lines) or "  (the game returned no outcome for this action)"

        if looked:
            view = channel.scene.render(state.watermark)
            state.watermark = channel.scene.updates
            body = f"{body}\n\n{view}"
        else:
            body = f"{body}\n\nThe scene did not arrive; observe again to see the result."
        return with_operator_messages(body, messages)

    async def capture_screen(step: int, thought: str, target_id: int | None = None) -> str:
        """Look at the actual picture on screen, not just the scene text.

        Use this when the step is about how something *looks*: a layout that may be
        broken, a button that may be covered by other UI, a sprite in the wrong
        state, text that may be unreadable. The scene listing cannot express any of
        that.

        Leave `target_id` out for the whole screen. Give one to see just that
        element's area, at higher detail.

        The picture arrives right after this result, as its own message.
        """
        if state.captures_attempted >= MAX_CAPTURES_PER_RUN:
            # Refused with the reason, not silently: a run that keeps looking
            # instead of deciding will reach the deadline with nothing reported.
            return (
                f"You have used all {MAX_CAPTURES_PER_RUN} screenshots for this run. "
                "Judge the remaining steps from the scene text."
            )

        await channel.note(thought, LogCategory.THOUGHT, step)
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
            return with_operator_messages(
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
            return with_operator_messages(
                f"The screen could not be captured — {item.error or 'no reason given'}. "
                "Judge this step from the scene text instead, and do not capture again "
                "in this run.",
                messages,
            )

        captured = item.returnValue or {}
        url = captured.get("url")
        if not url:
            return with_operator_messages(
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

        return with_operator_messages(f"Captured {what}. The image follows.", messages)

    async def click_button(step: int, target_id: int, thought: str) -> str:
        """Click a button. `target_id` must be an id from the scene you just saw.

        `step` is the scenario step this belongs to and `thought` is why you are
        clicking; both go on the timeline.
        """
        return await _run(
            JsonRpcAction(id=1, method="button_click", params=[target_id]),
            thought,
            f"Clicking {target_id}",
            step,
        )

    async def enter_text(step: int, target_id: int, value: str, thought: str) -> str:
        """Type into a text field. `target_id` must be an id from the current scene."""
        return await _run(
            JsonRpcAction(id=1, method="enter_text", params=[target_id, value]),
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
            JsonRpcAction(id=1, method="key_click", params=[key_code, duration_seconds]),
            thought,
            f"Pressing {key_code}",
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

    tools = [
        StructuredTool.from_function(coroutine=observe_scene, name="observe_scene"),
        StructuredTool.from_function(coroutine=click_button, name="click_button"),
        StructuredTool.from_function(coroutine=enter_text, name="enter_text"),
        StructuredTool.from_function(coroutine=press_key, name="press_key"),
        StructuredTool.from_function(coroutine=report_step, name="report_step"),
        StructuredTool.from_function(coroutine=finish_run, name="finish_run"),
        StructuredTool.from_function(coroutine=reply_to_operator, name="reply_to_operator"),
    ]

    # A model that cannot read images is not offered the tool at all. Left in, it
    # would be called, cost a game round trip, and produce an image nothing can
    # look at — and the agent would have no way to know why looking did not help.
    if supports_vision:
        tools.append(
            StructuredTool.from_function(coroutine=capture_screen, name="capture_screen")
        )
    return tools
