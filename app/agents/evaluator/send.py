"""Puts what the Evaluator decided on the wire, in the shapes Orchestration already records.

Nothing here judges or analyses. `send_verdict` takes a verdict and, for a failed step, its
`FailureReport`, builds the frames and emits them through the run's channel.

The verdict goes out the way the tool loop's `report_step` sends it — a per-step `STATUS` with
no `result`, the case id and whether the step is its case's verification step — so Orchestration
records both engines' steps the same way (`QaAgentInboundRouter.routeStatus`). A failed step's
analysis rides the same frame as `analysis`; Orchestration reads only `summary` and the citation
fields off a `STATUS` and stores the rest whole in `qa_log.payload`, so nothing on that side has
to change for the record to exist.

A failure the analysis classified as a game defect is also sent as an `ISSUE`, the frame the
tool loop's `report_issue` sends, so it lands in the project's defect list. The other three
categories stay on the `STATUS`: an unreached case or a step the engine stopped is not a defect
in the game, and the defect list is read by people looking for exactly those.
"""

from app.agents.evaluator.analysis import FailureReport
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaRunChannel
from app.qa.envelope import IssuePayload, MessageType, StatusPayload, StepStatus
from app.qa.schemas import QaScenario, QaStepResult


async def send_verdict(
    channel: QaRunChannel,
    state: QaRunState,
    step: int,
    passed: bool,
    message: str,
    analysis: FailureReport | None = None,
    expected: str = "",
) -> None:
    """Send one step's verdict as frames, with its analysis when the step failed and was analysed.

    `expected` is the result the step was to verify; it fills the `ISSUE` a `GAME_DEFECT`
    sends, which Orchestration stores whole beside the title.
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
        ),
    )
    if analysis is not None and analysis.is_defect:
        assert analysis.title and analysis.severity, "ground() refuses a defect without them"
        await channel.emit(
            MessageType.ISSUE,
            IssuePayload(
                title=analysis.title,
                severity=analysis.severity,
                step=step,
                expected=expected,
                actual=analysis.actual,
                reproduction=list(analysis.reproduction),
                category=analysis.category.value,
                cause=analysis.cause,
                insight=analysis.insight,
            ),
        )


def expected_text(scenario: QaScenario, step: int) -> str:
    """The result the step's test case expects, or an empty string for a step without one."""
    item = scenario.steps[step - 1]
    return item.case.expected if item.case and item.case.expected else ""
