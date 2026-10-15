"""The evaluator: judges a step after play stops, by running the step module's `check`.

`check` is written by the planner with the rest of the module, before any func runs, and loaded
in a namespace of its own (`executer.load_step_module`). It may use no other function of the
module (`sandbox.check`), so the verdict does not borrow what play is judged on. The verdict goes
out the way the agent's `report_step` sends it — a per-step `STATUS` with no `result`, the case id
and whether the step is its case's verification step — so Orchestration records both engines'
steps the same way.
"""

from app.agents.planner_executer.g import ExpectationFailed, GameHandle
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import MessageType, StatusPayload, StepStatus
from app.qa.schemas import QaStepResult


async def evaluate(g: GameHandle, check, stopped: str) -> tuple[bool, str]:
    """`check` 의 판정: 모든 `expect` 가 맞으면 통과. play 가 `done` 말고 다른 이유로 멈췄으면
    그 이유를 메시지에 붙인다."""
    note = "" if stopped == "done" else f" (executer stopped: {stopped})"
    g.last_expect = None
    try:
        await check(g)
    except QaCancelled:
        raise
    except ExpectationFailed as error:
        return False, f"Expected: {error}{note}"
    except Exception as error:  # noqa: BLE001 - a broken check is a failed verdict
        return False, f"CHECK-ERROR: {type(error).__name__}: {str(error)[:200]}"
    held = g.last_expect
    return True, (f"Held: {held}" if held else "Held (check asserted nothing).") + note


async def report(
    channel: QaRunChannel, state: QaRunState, step: int, passed: bool, message: str
) -> None:
    """Record and send one step's verdict, in the shape `report_step` sends it."""
    case_id, is_verification = (
        state.step_meta[step - 1] if 0 <= step - 1 < len(state.step_meta) else (None, False)
    )
    state.step_results.append(
        QaStepResult(
            step=step,
            passed=passed,
            message=message,
            case_id=case_id,
            is_verification=is_verification,
        )
    )
    await channel.emit(
        MessageType.STATUS,
        StatusPayload(
            status=StepStatus.COMPLETED if passed else StepStatus.FAILED,
            step=step,
            case_id=case_id,
            is_verification=is_verification,
            message=message,
        ),
    )
