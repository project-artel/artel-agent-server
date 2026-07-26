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
    async def observe_scene(wait_seconds: float = 0.0, reason: str = "checking the screen") -> str:
        """Look at the game screen. Returns what changed since your last look.

        Use `wait_seconds` when the screen needs time first — a loading screen, an
        animation, a countdown. Always look before acting, and look again after
        acting to see what your action did.
        """
        received = await channel.request_scene(wait_seconds, reason)
        messages = channel.drain_operator_messages()
        if received is None:
            return with_operator_messages(
                "The game did not answer. It may be loading, or it may be stuck. "
                "You can look again with a longer wait, or judge the step failed.",
                messages,
            )
        view = channel.scene.render(state.watermark)
        state.watermark = channel.scene.updates
        return with_operator_messages(view, messages)

    async def perform_actions(actions: list[dict[str, Any]], message: str) -> str:
        """Run actions on the game and return each one's outcome.

        Each action is `{"method": ..., "target_id": ..., "arguments": [...]}`.
        `target_id` must be an id present in the scene you just observed; omit it
        for methods that take none. `message` is one short line for the operator.
        """
        planned: list[JsonRpcAction] = []
        for index, action in enumerate(actions):
            method = str(action.get("method", "")).strip()
            if not method:
                return "Every action needs a method. Nothing was run."
            target = action.get("target_id")
            arguments = list(action.get("arguments") or [])
            planned.append(
                JsonRpcAction(
                    id=index + 1,
                    method=method,
                    params=([] if target is None else [target]) + arguments,
                )
            )
        if not planned:
            return "No actions were given, so nothing was run."

        result = await channel.dispatch_actions(planned, message)
        messages = channel.drain_operator_messages()
        if result is None:
            return with_operator_messages(
                "The game did not report a result. It may still have run. "
                "Observe the scene to find out what actually happened.",
                messages,
            )
        lines = [
            f"  action {item.id}: {item.status.value}"
            + (f" — {item.error}" if item.error else "")
            for item in result.results
        ]
        body = "results:\n" + "\n".join(lines) if lines else "The game reported no per-action result."
        return with_operator_messages(body, messages)

    async def report_step(step: int, passed: bool, message: str) -> str:
        """Record the verdict for one scenario step, with the evidence for it.

        Call this once per step, right after you have observed the result of that
        step's action. `message` should cite what you saw.
        """
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

    async def finish_run(passed: bool, summary: str) -> str:
        """End the run. Call this once, after the last step has been reported."""
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

    async def reply_to_operator(message: str) -> str:
        """Answer the operator. Use when they asked something, not for progress."""
        await channel.say(message)
        return "Sent."

    return [
        StructuredTool.from_function(coroutine=observe_scene, name="observe_scene"),
        StructuredTool.from_function(coroutine=perform_actions, name="perform_actions"),
        StructuredTool.from_function(coroutine=report_step, name="report_step"),
        StructuredTool.from_function(coroutine=finish_run, name="finish_run"),
        StructuredTool.from_function(coroutine=reply_to_operator, name="reply_to_operator"),
    ]
