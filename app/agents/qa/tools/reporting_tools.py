"""TC 검증 시도별 판정과 사용자 질문을 기록한다. step은 참고 절차다."""

from typing import Literal
from dataclasses import replace
import httpx

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.agents.qa.case_context import render_case
from app.agents.qa.question_policy import ASK_USER_DESCRIPTION
from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.game_function_tools import focus, stop_not_allowed_before, active_behaviors
from app.agents.qa.macro.lift import draft_for_step, offer
from app.prompts import load_tool_description
from app.qa.envelope import IssuePayload, IssueSeverity, MessageType, RunResult, StatusPayload, StepStatus
from app.qa.schemas import QaCaseAttemptResult
from app.qa.verdict_evidence import VerdictCheck, cite, contradiction


def render_closing_asks(state: QaRunState) -> str:
    return "TC 판정은 기록됐습니다. TC와 무관한 결함만 report_issue로 보고하세요. 다음 QA에 도움이 될 발견은 record_knowledge로 남길 수 있습니다."


def build_reporting_tools(ctx: ToolContext) -> list[BaseTool]:
    channel, state, arch = ctx.channel, ctx.state, ctx.arch
    _answer = ctx.answer
    state.cites_captures = arch.verdict_evidence == "on"
    dispatch_starts: dict[int, int] = {}
    cheat_starts: dict[int, int] = {}
    pending_reports: dict[int, tuple] = {}

    def reported_case_ids() -> set[int]:
        return {result.case_id for result in state.case_attempt_results if result.verdict != "INTERRUPTED"}

    def draft_for_attempt(case_id: int, start: int) -> str:
        step = state.step_of_case(case_id)
        if arch.macros != "on" or step is None:
            return ""
        records = [replace(record, step=step) for record in state.dispatches[start:]
                   if record.step is None or state.case_id_at(record.step) == case_id]
        taken = set(state.macros.drafts) | set(state.macros.registrations)
        snapshot = channel.evaluation.snapshots.get(case_id, {})
        description = snapshot.get("test_step") or "; ".join(
            text for index, text in enumerate(state.step_texts, start=1)
            if state.case_id_at(index) == case_id
        )
        draft, _ = draft_for_step(records, step, taken, description)
        if draft is None:
            return ""
        state.macros.write(draft.name, draft.source)
        state.macros.remember_read(draft.name)
        state.offered_drafts[step] = draft.name
        if state.phase_cycle is not None:
            state.phase_cycle.expect_draft_review()
        return offer(draft)

    @tool(description="이 시나리오의 TC ID로 실행 당시 원문 snapshot을 조회한다. 사전 조건, 테스트 절차, 기대 결과와 제공된 사람 피드백을 읽는다. 모든 phase에서 사용할 수 있으며 검증 시도를 시작하거나 변경하지 않는다.")
    async def get_test_case(case_id: int, thought: str) -> str:
        return render_case(case_id, state, channel)

    @tool(description="Start a verification attempt for a scenario test case and read its snapshot. Start a new attempt when retesting the same case. In ORDERED scenarios, this tool warns about earlier cases without verdicts. Reference steps do not require separate verdicts.")
    async def start_test_case(case_id: int, thought: str) -> str:
        step = state.step_of_case(case_id)
        if step is None:
            return "This test case is not assigned to the scenario."
        stopped = await stop_not_allowed_before(ctx, step, starting_case=True)
        try:
            attempt = await channel.evaluation.start(case_id)
        except httpx.HTTPError:
            return "Could not save the start of the verification attempt. Retry the same start_test_case call."
        state.current_case_id = case_id
        dispatch_starts.setdefault(case_id, len(state.dispatches))
        cheat_starts.setdefault(case_id, len(state.used_cheats))
        if arch.cheats == "on" or arch.behaviors == "on":
            focus(ctx, step)
        warning = ""
        if state.execution_mode == "ORDERED":
            earlier = dict.fromkeys(cid for cid, _ in state.step_meta[:step - 1] if cid is not None)
            reported = reported_case_ids()
            skipped = [str(cid) for cid in earlier if cid not in reported]
            if skipped:
                warning = f"\nWarning: Earlier test cases still need verdicts: {', '.join(skipped)}."
        return _answer(f"Started verification attempt {attempt.id} for test case {case_id}. Call report_test_case after verification." + warning + stopped
                       + "\n" + render_case(case_id, state, channel), channel.drain_operator_messages())

    @tool(description="시작한 TC 검증 시도의 결과를 PASSED/FAILED/INCONCLUSIVE로 보고한다. step별 판정은 하지 않는다. 반복 검증은 새 시도로 보존한다. TC 실패 issue는 자동 생성된다. 판단할 증거가 부족하면 INCONCLUSIVE를 사용한다.")
    async def report_test_case(case_id: int, verdict: Literal["PASSED", "FAILED", "INCONCLUSIVE"],
        message: str, thought: str, checks: list[VerdictCheck] = [], used_knowledge_ids: list[str] = []) -> str:
        attempt_id = channel.evaluation.attempts.get(case_id)
        if attempt_id is None:
            if state.phase_cycle is not None:
                state.phase_cycle.hold()
            return "먼저 이 TC에 대해 start_test_case를 호출하세요."
        pending = pending_reports.get(case_id)
        if pending is not None and pending[1] != verdict:
            if state.phase_cycle is not None:
                state.phase_cycle.hold()
            return f"저장 응답을 기다리는 {pending[1]} 판정이 있습니다. 같은 결과로 재시도하세요."
        if verdict != "INCONCLUSIVE" and arch.verdict_evidence == "on":
            problem = "기대 결과와 관측 결과를 checks에 작성하세요." if not checks else contradiction(verdict == "PASSED", checks)
            if problem:
                if state.phase_cycle is not None:
                    state.phase_cycle.hold()
                return f"판정을 기록하지 않았습니다. {problem}"
        cited_checks, evidence = None, None
        if checks:
            pulse = channel.scene.pulse
            cited_checks, evidence = cite(checks, pulse, state.shown_captures, pulse.scene or channel.scene.scene)
        citations = [entry for entry in dict.fromkeys(used_knowledge_ids) if state.knows_of(entry)]
        rejected_citations = len(set(used_knowledge_ids) - set(citations))
        if pending is None:
            used_cheats = [entry.model_dump(mode="json") for entry in state.used_cheats[cheat_starts.get(case_id, 0):]] if arch.cheats == "on" else None
            behaviors = [entry.model_dump(mode="json") for entry in active_behaviors(ctx)] if arch.behaviors == "on" else None
            pending = pending_reports[case_id] = (case_id, verdict, message, thought, cited_checks, evidence, citations, used_cheats, behaviors, rejected_citations)
        try:
            await channel.evaluation.report(*pending)
        except httpx.HTTPError:
            if state.phase_cycle is not None:
                state.phase_cycle.hold()
            return "TC 판정 저장 응답을 확인하지 못했습니다. 같은 결과로 report_test_case를 재시도하세요."
        pending_reports.pop(case_id, None)
        state.case_attempt_results.append(QaCaseAttemptResult(attempt_id=str(attempt_id), case_id=case_id,
            verdict=verdict, message=pending[2]))
        if state.current_case_id == case_id:
            state.current_case_id = None
        start = dispatch_starts.pop(case_id, len(state.dispatches))
        cheat_starts.pop(case_id, None)
        draft = draft_for_attempt(case_id, start) if verdict == "PASSED" else ""
        body = f"TC {case_id} 검증 시도를 {verdict}로 기록했습니다. " + render_closing_asks(state) + draft
        if pending[9]:
            body += f"\n\n{pending[9]} of the ids you cited are not entries this run has been shown, so they were not recorded. The verdict stands. Cite only ids printed by a search or a neighbour line."
        if not arch.report_step_view:
            deferred = channel.scene.pulse.defer_view()
            if deferred:
                body += f"\n\n{deferred}"
        return _answer(body, channel.drain_operator_messages(), screen=arch.report_step_view)

    @tool(description=ASK_USER_DESCRIPTION)
    async def ask_user(question: str, thought: str) -> str:
        answer = await channel.evaluation.ask(question, lambda: channel.cancelled)
        channel.operator_instructions.append(answer)
        return _answer("사용자가 답변했습니다.", [answer])

    @tool(
        description="TC와 관련 없는 별도 결함만 보고한다. TC 실패 issue는 자동 생성된다. " + load_tool_description("report_issue").body.format(
            severities="/".join(s.value for s in IssueSeverity),
            limit=arch.max_issues_per_run,
        )
    )
    async def report_issue(
        severity: str,
        title: str,
        expected: str,
        actual: str,
        reproduction: list[str],
        thought: str,
        step: int | None = None,
    ) -> str:
        if state.issues_attempted >= arch.max_issues_per_run:
            return (
                f"You have filed all {arch.max_issues_per_run} issues this run "
                "allows. Nothing was sent. Carry the remaining findings in the "
                "run summary instead."
            )
        # Both required fields are checked here rather than left to Orchestration,
        # and for the same reason: a frame with a blank title or an unknown
        # severity is dropped there without a reply, so an agent that got either
        # wrong would go on believing it had reported the defect.
        if not title.strip():
            return (
                "An issue needs a title — one line naming the defect — so nothing "
                "was filed. Call this again with one."
            )
        try:
            checked = IssueSeverity(severity.strip().upper())
        except ValueError:
            allowed = "/".join(s.value for s in IssueSeverity)
            return (
                f"'{severity}' is not a severity, so nothing was filed. Call this "
                f"again with one of {allowed}."
            )
        state.issues_attempted += 1
        await channel.emit(
            MessageType.ISSUE,
            IssuePayload(
                title=title,
                severity=checked,
                step=step,
                expected=expected,
                actual=actual,
                reproduction=reproduction,
            ),
        )
        remaining = arch.max_issues_per_run - state.issues_attempted
        return _answer(
            f"Filed as {checked.value}. {remaining} issue(s) left this run.",
            channel.drain_operator_messages(),
        )

    # 판정이 빠져 있으면 tool 결과를 모델에 돌려보내고, 종료가 성공하면 runner가 finished를 확인한다.
    @tool(description="Close the QA run after recording a verdict for every assigned test case with report_test_case. Report INCONCLUSIVE when evidence is insufficient. Reference steps do not require separate verdicts.")
    async def finish_run(passed: bool, summary: str, thought: str) -> str:
        case_ids = dict.fromkeys(case_id for case_id, _ in state.step_meta if case_id is not None)
        if not case_ids:
            return "Cannot finish the run because no test cases were assigned. Ask the operator to check the test case assignment."
        reported = reported_case_ids()
        missing = [str(case_id) for case_id in case_ids if case_id not in reported]
        if missing:
            return (
                f"Cannot finish the run. Test cases without a saved verdict: {', '.join(missing)}. "
                "Record each verdict with report_test_case. Use INCONCLUSIVE when evidence is insufficient."
            )
        state.finished = True
        await channel.emit(MessageType.STATUS, StatusPayload(status=StepStatus.COMPLETED,
            result=RunResult.PASSED if passed else RunResult.FAILED, message=summary, summary=state.build_summary()))
        return "The QA run is closed."

    @tool(description=load_tool_description("reply_to_operator").body)
    async def reply_to_operator(message: str, thought: str, step: int | None = None) -> str:
        await channel.say(message, step)
        return _answer("전송했습니다.", channel.drain_operator_messages())

    return [start_test_case, report_test_case, ask_user, report_issue, finish_run, reply_to_operator, get_test_case]
