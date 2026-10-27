"""Sends what the Evaluator decided, in the shapes Orchestration already records.

The verdict goes out the way the tool loop's `report_step` sends it — a per-step `STATUS` with
no `result`, the case id and whether the step is its case's verification step — so Orchestration
records both engines' steps the same way (`QaAgentInboundRouter.routeStatus`).
"""

from app.agents.qa.tools import QaRunState
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType, StatusPayload, StepStatus
from app.qa.schemas import QaStepResult


async def report(
    channel: QaRunChannel, state: QaRunState, step: int, passed: bool, message: str
) -> None:
    """Record and send one step's verdict, in the shape `report_step` sends it."""
    case_id, is_verification = (
        state.step_meta[step - 1] if 0 <= step - 1 < len(state.step_meta) else (None, False)
    )
    state.step_results.append(
        QaStepResult(
            step=step, passed=passed, message=message, case_id=case_id, is_verification=is_verification
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
