"""Puts what the Evaluator decided on the wire, in the shape Orchestration already records.

Nothing here judges or analyses. `send_verdict` takes a verdict and, for a failed step, its
`FailureReport`, builds the frame and emits it through the run's channel.

One frame per step, a `STATUS`, the way the tool loop's `report_step` sends it — no `result`,
the case id and whether the step is its case's verification step — so Orchestration records
both engines' steps the same way (`QaAgentInboundRouter.routeStatus`). A failed step's analysis
rides the same frame as `analysis`; Orchestration reads only `summary` and the citation fields
off a `STATUS` and stores the rest whole in `qa_log.payload`.

No `ISSUE` leaves here. A failed case is not a defect until a person approves it as one; that
approval happens in Orchestration, which builds the issue from this `analysis` — the title and
severity it suggests, the cause, the reproduction — and the recording of the step. An agent that
filed issues on its own put unreached cases and inconclusive evidence in the defect list next to
real defects, which is the list the approval exists to keep clean.
"""

from app.agents.evaluator.analysis import FailureReport
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType, StatusPayload, StepStatus
from app.qa.schemas import QaStepResult


async def send_verdict(
    channel: QaRunChannel,
    state: QaRunState,
    step: int,
    passed: bool,
    message: str,
    analysis: FailureReport | None = None,
    played: dict | None = None,
) -> None:
    """Record and send one step's verdict, with its analysis when the step failed and was analysed.

    `played` is `StepPlay.played()` — the frames and the clock the play ran between — for the
    recording to be cut by. A step that was never played sends none.
    """
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
            analysis=analysis.as_payload() if analysis is not None else None,
            played=played,
        ),
    )
