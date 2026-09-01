"""판정을 내고 런을 닫는 도구, 그리고 오퍼레이터와 주고받는 도구.

`report_step` 은 스텝 하나의 판정을, `report_issue` 는 게임의 결함을, `finish_run` 은 런
전체의 판정을 낸다. `wait_for_operator` 와 `reply_to_operator` 는 사람과의 왕복이다.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import bounded_operator_wait
from app.qa.envelope import (
    IssuePayload,
    IssueSeverity,
    MessageType,
    RunResult,
    StatusPayload,
    StepStatus,
)
from app.qa.schemas import QaStepResult


# Interpolated for the same reason as the capture description above: the cap is
# the part the agent has to ration, and a docstring cannot name it.
REPORT_ISSUE_DESCRIPTION = """File a defect you found in the GAME, with the evidence for it.

This is not the step verdict — `report_step` is. Use this when what you saw is
wrong regardless of the scenario: a crash, a button that does nothing, a value
that goes the wrong way, text that overflows its box. A step can fail without
being a defect (the scenario may describe the game wrongly), and a step can pass
while you notice one in passing.

`severity` is one of {severities}, worst first:
- BLOCKER: the game cannot be played past this point.
- CRITICAL: a core feature is broken and nothing works around it.
- MAJOR: an important feature is broken but there is a way around it.
- MINOR: small or local damage.
- TRIVIAL: cosmetic — wording, alignment, a wrong colour.

`expected` and `actual` are what should have happened and what did. `reproduction`
is the shortest list of steps that shows it again, oldest first; write it for
someone who was not here.

You may file {limit} of these in one run. One defect, one call — do not file the
same broken screen again on the next step."""


def render_closing_asks(state: QaRunState) -> str:
    """마지막 스텝을 판정한 자리에서, 이 런이 무엇을 남기고 닫을지 묻는 문구.

    런 전체가 아직 앞에 있고 판정은 끝나서, 무엇이 값진 앎이었고 무엇이 결함이었는지
    되짚을 수 있는 마지막 순간이다.

    도구는 있는데 안 쓴다 — 실측으로 83턴 성공 런에서 `record_knowledge` 0회,
    `report_issue` 0회였다. 시스템 프롬프트가 시키는데도 그렇다. 기록은 이번 런의 판정에
    아무것도 안 보태므로(`report_step` 도 `finish_run` 도 그것 없이 통과한다) 비용만 있고
    돌아오는 것이 없는 행동이고, 무엇보다 **적을 순간이 흐름 안에 없었다**.

    되돌려 보내지 않는다. `finish_run` 에서 한 번 물리는 것도 해 봤는데, 런을 닫는 테스트
    열여덟 개가 걸렸다 — 앞으로 만드는 모든 런 테스트가 그 왕복을 치러야 한다는 뜻이고, 그
    값은 이 문구가 하는 일보다 크다(ARTEL-667).

    무엇을 어디에 적을지는 안 정해 준다. `record_knowledge` 가 anchor 를 부르는 쪽에
    맡기는 이유가 그대로 걸린다 — 어디서나 참인 규칙을 지금 서 있는 화면 아래 넣으면 다음
    화면의 런이 그것을 못 찾는다.

    결함을 묻는 근거는 **이 런이 실패로 판정한 스텝**이다. 실패한 스텝이 하나도 없으면
    결함은 묻지 않는다 — 근거 없이 물으면 agent 가 무엇에 대해 답할지 모르고, 그래도
    답하려고 아무거나 적으면 다음 런에 도움이 안 되는 줄만 쌓인다. 같은 이유로 마지막 줄이
    적을 것이 없다는 것도 답이라고 말한다.

    이미 시도한 런에게도 말한다. 한 번 시도한 것과 그 런이 알아낸 것을 다 적은 것은
    다르다. 대신 문구가 다르다 — 시키는 대신 지금까지 몇 번 시도했는지 세어 주고
    각 시도의 성공 여부와 나머지를 되짚게 한다. 이 counter 둘은 성공 건수가 아니다.
    """
    asks: list[str] = []
    failed = [result.step for result in state.step_results if not result.passed]
    if failed:
        steps = ", ".join(str(step) for step in failed)
        if not state.issues_attempted:
            asks.append(
                f"Steps judged failed this run: {steps}. Issue reports sent: none, so "
                "what failed and why is nowhere but this transcript. A failure "
                "that is a defect in the game goes in with `report_issue`; a step "
                "that failed because the scenario asked for the wrong thing, or "
                "because the run itself went wrong, is not one."
            )
        else:
            asks.append(
                f"Steps judged failed this run: {steps}. Issue reports sent: "
                f"{state.issues_attempted}. If a failure that is a defect in the "
                "game is not among them, `report_issue` still takes it."
            )
    if not state.knowledge_records_attempted:
        asks.append(
            "If this run worked anything out that a later run would otherwise "
            "work out again — how an input is read, what a control actually does, "
            "what a rule costs — write it down with `record_knowledge`."
        )
    else:
        asks.append(
            f"Knowledge recording attempts this run: {state.knowledge_records_attempted}. "
            "Check whether each one succeeded. An attempt is not the same as "
            "everything this run worked out. Read back over the rest of it: what "
            "else would a later run otherwise work out again? That goes in with "
            "`record_knowledge` too."
        )
    listed = "\n".join(f"- {ask}" for ask in asks)
    closer = (
        "Nothing to write is an answer. If there is nothing a later run would "
        "use, write nothing and close the run."
    )
    return f"\n{listed}\n{closer}"


def build_reporting_tools(ctx: ToolContext) -> list[BaseTool]:
    # ctx 가 든 것을 여기서 되묶는다. 아래 tool 은 `build_tools` 한 함수 안에 있던 것을
    # 그대로 옮긴 것이라, 이 줄이 있어야 본문이 한 글자도 바뀌지 않는다. 읽는 쪽에는
    # 아래 tool 이 무엇을 closure 로 잡는지 먼저 말해 주는 머리말이기도 하다.
    channel, state, arch = ctx.channel, ctx.state, ctx.arch
    _answer = ctx.answer

    @tool
    async def wait_for_operator(
        thought: str, timeout_seconds: float = 60.0, step: int | None = None
    ) -> str:
        """Stop and wait until the operator says something.

        For when you cannot go on without a person: the step is ambiguous, the
        game is in a state the scenario does not cover, or you asked them
        something with `reply_to_operator` and the answer decides what you do
        next. The run makes no progress while you are here, so ask the question
        first and only then wait for it.

        Returns what they said, or tells you nobody answered in time — silence is
        not a failure, and you decide what to do with it. But do not settle in on
        it: waiting is capped per call, and a couple of full waits is the whole
        run's clock, spent on nothing.
        """
        waited = bounded_operator_wait(timeout_seconds)
        messages = await channel.wait_for_operator(timeout_seconds)
        if not messages:
            return (
                f"The operator said nothing within {waited:g}s. Decide for "
                "yourself whether to wait again, carry on with what you have, or "
                "judge the step failed."
            )
        return _answer("The operator answered.", messages)

    @tool
    async def report_step(
        step: int,
        passed: bool,
        message: str,
        thought: str,
        used_knowledge_ids: list[str] = [],
    ) -> str:
        """Record the verdict for one scenario step, with the evidence for it.

        Call this once per step, right after you have observed the result of that
        step's action. For a step that verifies an expected result, `passed` is
        whether that result occurred (this is also its test case's verdict); for
        any other step, `passed` is whether you carried the action out. `message`
        should cite what you saw, and `thought` is how you reached the verdict.

        `used_knowledge_ids` is for knowledge base entries that actually bore on
        THIS verdict — ones you read and then judged differently because of. Give
        the ids as they were printed to you, whether by a search or as a neighbour
        line. Leave it empty when the step was decided by what you could see;
        that is the ordinary case and nothing is lost by saying so.
        """
        # The empty list default is never mutated — the ids are read once, below.
        # It is spelled as a literal rather than as `None` because this is a tool
        # schema the model fills in: an optional array is something it can simply
        # omit, while a nullable one invites it to send `null` and then wonder
        # whether that meant "none" or "unknown".
        # `knows_of`, NOT `knowledge_seen`. An entry shown only as a one-line
        # neighbour can still be what a verdict rested on, and citing it destroys
        # nothing. `knowledge_seen` is the bar for `update_knowledge` and
        # `forget_knowledge` because those DO destroy something, and a 120-character
        # line is not having read the entry — that boundary is deliberate and this
        # tool sits on the other side of it.
        #
        # Duplicates are folded first: citing one entry twice is one citation, and
        # counting it twice would make "how much knowledge this verdict used" a
        # function of how the model happened to phrase the list.
        cited: list[str] = []
        rejected: list[str] = []
        for entry in dict.fromkeys(used_knowledge_ids):
            (cited if state.knows_of(entry) else rejected).append(entry)
        # 이 스텝이 어느 TC에 속하고 그 구간의 검증 스텝인지를 판정에 붙인다(2단 판정). step_meta가
        # 없으면(구식 호출자) 미상으로 둔다.
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
        # The verdict frame stays a PER-STEP one: `result` is left null, so
        # Orchestration's routeStatus logs it and the run goes on. Citations ride
        # along here rather than in a frame of their own precisely so they cannot
        # change that — a second frame type would be a second thing to get wrong
        # about ending the run.
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED if passed else StepStatus.FAILED,
                step=step,
                case_id=case_id,
                is_verification=is_verification,
                message=message,
                used_knowledge_ids=cited,
                rejected_knowledge_id_count=len(rejected),
            ),
        )
        remaining = state.total_steps - len(state.step_results)
        # Said out loud, not dropped. The verdict itself is already recorded, so
        # this is not a refusal — but an agent told nothing would carry on
        # believing the entry was credited, and the ids it invents are exactly
        # what nobody would otherwise notice.
        note = (
            ""
            if not rejected
            else (
                f"\n\n{len(rejected)} of the ids you cited are not entries this run "
                f"has been shown, so they were not recorded: {rejected}. The verdict "
                "stands. Cite only ids printed to you by a search or a neighbour line."
            )
        )
        if remaining <= 0:
            # 무엇을 남길지 묻는 자리이자 이유는 `render_closing_asks` 가 들고 있다. 여기서
            # 말하는 것은 그 자리가 여기라는 것뿐이다 — 매 스텝마다 붙이면 표가 뜻을 잃고,
            # `finish_run` 은 이미 닫는 쪽으로 기운 뒤다.
            return _answer(
                "Recorded. This step report does not close the run. That was the "
                "last step — when you are done with the follow-up work below, "
                f"call `finish_run` yourself:{render_closing_asks(state)}{note}",
                channel.drain_operator_messages(),
            )
        # The verdict is recorded either way; what differs is the pull to keep
        # going. A failure is where the loop is most tempted to call it a day, so
        # that is where the next move has to be spelled out rather than implied.
        body = f"Recorded. {remaining} step(s) left — continue with step {step + 1}."
        if not passed:
            body = f"{body} A failed step is not a reason to stop."
        return _answer(f"{body}{note}", channel.drain_operator_messages())

    @tool(
        description=REPORT_ISSUE_DESCRIPTION.format(
            severities="/".join(s.value for s in IssueSeverity),
            limit=arch.max_issues_per_run,
        )
    )
    async def report_issue(
        step: int,
        severity: str,
        title: str,
        expected: str,
        actual: str,
        reproduction: list[str],
        thought: str,
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

    @tool
    async def finish_run(passed: bool, summary: str, thought: str) -> str:
        """End the run. Call this once, after the last step has been reported.

        A step with no verdict yet is worth attempting before you close: the run
        was opened to find out about all of them. Calling this with steps still
        unreported sends you back to them once.

        `thought` is how you reached the overall verdict; it goes on the timeline.
        """
        state.finish_attempts += 1

        # A step the agent never attempted is the failure this whole change is
        # about, so closing over one costs a round trip. Only the first, though:
        # the second call closes whatever the state, because a run the game has
        # abandoned still has to be able to end.
        unreported = state.unreported_steps()
        if unreported and state.finish_attempts == 1:
            listed = ", ".join(str(step) for step in unreported)
            return (
                f"{len(unreported)} step(s) still have no verdict: {listed}. Go "
                "attempt them — a step you have not tried may still pass. If the "
                "game truly cannot go on, report them failed with the reason, then "
                "call `finish_run` again."
            )

        state.finished = True
        await channel.emit(
            MessageType.STATUS,
            StatusPayload(
                status=StepStatus.COMPLETED,
                result=RunResult.PASSED if passed else RunResult.FAILED,
                message=summary,
                summary=state.build_summary(),
            ),
        )
        return "The run is closed."

    @tool
    async def reply_to_operator(message: str, thought: str, step: int | None = None) -> str:
        """Answer the operator. Use when they asked something, not for progress.

        `thought` is why you are answering this way; it goes on the timeline so a
        reviewer can see the reasoning behind what the operator was told.
        """
        await channel.say(message, step)
        return _answer("Sent.", channel.drain_operator_messages())

    return [
        wait_for_operator,
        report_step,
        report_issue,
        finish_run,
        reply_to_operator,
    ]
