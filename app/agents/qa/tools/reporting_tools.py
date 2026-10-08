"""판정을 내고 런을 닫는 도구, 그리고 오퍼레이터와 주고받는 도구.

`report_step` 은 스텝 하나의 판정을, `report_issue` 는 게임의 결함을, `finish_run` 은 런
전체의 판정을 낸다. `wait_for_operator` 와 `reply_to_operator` 는 사람과의 왕복이다.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.macro.lift import draft_for_step, offer
from app.agents.qa.tools.phase import MAX_CONSECUTIVE_REFUSALS
from app.agents.qa.tools.state import QaRunState
from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_tool_description
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
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state, arch = ctx.channel, ctx.state, ctx.arch
    _answer = ctx.answer

    @tool(description=load_tool_description("wait_for_operator").body)
    async def wait_for_operator(
        thought: str, timeout_seconds: float = 60.0, step: int | None = None
    ) -> str:
        waited = bounded_operator_wait(timeout_seconds)
        messages = await channel.wait_for_operator(timeout_seconds)
        if not messages:
            return (
                f"The operator said nothing within {waited:g}s. Decide for "
                "yourself whether to wait again, carry on with what you have, or "
                "judge the step failed."
            )
        return _answer("The operator answered.", messages)

    def _capability_key_problem(capability_key: str) -> str | None:
        """이 런이 이 키를 받은 적이 있나. 없으면 무엇이 잘못됐는지 한 줄.

        보는 곳이 둘이다 — `list_scene_capabilities` 가 찍어 준 줄(`state`), 그리고 지금 서
        있는 씬의 맥락 block 이 찍은 줄. `used_knowledge_ids` 가 `state.knows_of` 로 하는 것과
        같은 검사이고, 이유도 같다: 지어낸 키도 저쪽에서는 진짜 행을 가리키고, 저쪽은 그것이
        이 런이 본 행인지 알 방법이 없다.

        서 있지 않은 씬의 키는 여기서 통과하지 못한다. `record_capability_verdict` 가 이미
        같은 선을 긋고 있고(저쪽이 서 있지 않은 씬의 verdict 를 거절한다), 서 있지 않은 씬은
        이 런이 지금 보고 있지 않은 씬이다.
        """
        if state.was_shown_capability_key(capability_key):
            return None
        context = channel.scene.scene_context
        scene = (channel.scene.scene or channel.scene.pulse.scene or "").strip()
        entry = context.entry_for(scene) if context is not None else None
        printed = entry.printed_capabilities() if entry is not None else []
        if any(item.capability_key == capability_key for item in printed):
            return None
        return (
            f"`capability_key` {capability_key!r} is not a row this run has been shown, "
            "so it was not recorded against this step. The verdict stands. Keys come "
            "from the square brackets on a capability line in your scene context block "
            "or in a `list_scene_capabilities` result — call that tool to find the row "
            "you mean."
        )

    def _macro_draft(step: int, passed: bool) -> str:
        """이 step 에 손으로 보낸 것을 macro 초안으로 만들어 내민다. macro 를 켠 런에만.

        판정이 받아들여진 응답에 붙는다 — `UPDATE_MEMORY` 로 넘어가는 그 순간에 agent 가
        읽는다. 초안은 `MacroBook` 에 써 두므로 agent 가 할 일은 `register_macro` 한 번이다.
        만들지 못했으면 아무 말도 안 붙인다(`app/agents/qa/macro/lift.py`).

        **통과한 판정에만 붙는다.** 실패한 step 에 보낸 것은 게임이 받아 주지 않은 순서라
        macro 가 되면 안 된다. 실패 판정은 `drafted_steps` 에도 올리지 않는다 — 같은 step 을
        나중에 통과로 다시 판정하면 그때 초안을 낼 수 있어야 한다.

        내민 초안은 읽은 것으로 친다. 초안 원문이 응답에 그대로 실렸으므로 `edit_macro` 가
        `read_macro` 를 또 요구하면 고쳐서 등록할 길이 막힌다.
        """
        if arch.macros != "on" or not passed or step in state.drafted_steps:
            return ""
        state.drafted_steps.add(step)
        taken = set(state.macros.drafts) | set(state.macros.registrations)
        # 시나리오 밖의 번호(모델이 지어낸 step)는 문장이 없다. 범위를 보고 빈 문자열로 둔다.
        in_range = 1 <= step <= len(state.step_texts)
        draft, _why = draft_for_step(
            state.dispatches, step, taken, state.step_texts[step - 1] if in_range else ""
        )
        if draft is None:
            return ""
        state.macros.write(draft.name, draft.source)
        state.macros.remember_read(draft.name)
        state.offered_drafts[step] = draft.name
        return offer(draft)

    async def _record_verdict(
        step: int,
        passed: bool,
        message: str,
        used_knowledge_ids: list[str],
        capability_key: str | None = None,
        learned: str | None = None,
        asks_memory: bool = False,
    ) -> str:
        """두 `report_step` 모양이 함께 쓰는 몸통.

        `asks_memory` 는 `phase_cycle` 이 `in_verdict` 이상인가다. `off` 에서는 아래 두 인자가
        schema 에 아예 없으므로 이 갈래는 한 줄도 안 돈다 — 기존 런 테스트가 왕복을 한 번도
        더 치르지 않아야 한다는 것이 이 축의 조건이다(ARTEL-667).
        """
        notes: list[str] = []

        # `learned` 를 안 받았으면 되돌려 보낸다. 판정을 적기 **전**이다 — 뒤에 두면 다시
        # 부른 호출이 같은 스텝의 판정을 두 번 쌓는다.
        #
        # 되돌려 보내는 것은 인자를 진짜 질문으로 만드는 유일한 방법이다. 빠뜨린 것을 말만
        # 하고 지나가면 v16 이 문장으로 부탁하고 0 을 받은 그 실패를 그대로 재현한다. 대신
        # 연속 상한을 둔다 — phase 거절과 같은 산수이고(`phase.py` 의
        # `MAX_CONSECUTIVE_REFUSALS`), 상한에 닿으면 통과시키고 답이 없었다고 적는다.
        if asks_memory and learned is None:
            if state.verdict_memory_refusals < MAX_CONSECUTIVE_REFUSALS:
                state.verdict_memory_refusals += 1
                # `lite`·`full` 에서는 이 호출로 `VERIFY` 가 끝나면 안 된다. 판정이 하나도
                # 안 적혔는데 다음 phase 로 가면 `UPDATE_MEMORY` 가 없는 판정을 두고 묻는다.
                if state.phase_cycle is not None:
                    state.phase_cycle.hold()
                return (
                    "Nothing was recorded — this report has no `learned`. Call "
                    "`report_step` again with the same verdict and add it: one line a "
                    'later run would otherwise work out again, or `learned: ""` to say '
                    "this step left nothing worth keeping. The empty string is a real "
                    "answer; leaving the argument out is not."
                )
            state.verdict_memory_refusals = 0
            notes.append(
                f"You have now left `learned` out {MAX_CONSECUTIVE_REFUSALS} times in a "
                "row, so this verdict was recorded without it and the run is going on. "
                "It stands on the record as unanswered, which is not the same as "
                "nothing to write."
            )
        else:
            state.verdict_memory_refusals = 0

        key = (capability_key or "").strip()
        if asks_memory and key:
            problem = _capability_key_problem(key)
            if problem is not None:
                notes.append(problem)

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
        #
        # `capability_key` 와 `learned` 도 같은 이유로 프레임에 안 실린다. 둘은 모델이 쓴
        # 인자 그대로 `qa_log` 의 tool 호출 행에 남고, 파일럿이 세는 자리가 거기다.
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
        if rejected:
            notes.append(
                f"{len(rejected)} of the ids you cited are not entries this run "
                f"has been shown, so they were not recorded: {rejected}. The verdict "
                "stands. Cite only ids printed to you by a search or a neighbour line."
            )
        note = "".join(f"\n\n{line}" for line in notes) + _macro_draft(step, passed)
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

    # 두 모양을 `if` 로 가른다. 인자 하나를 `None` 기본값으로 늘 달아 두는 길도 있지만, 그러면
    # `off` 런의 tool schema 가 움직이고 `arch_fingerprint` 가 그것을 tool 의 `args` 로 잡는다
    # — 아무것도 안 켠 런이 다른 구조로 기록되는 것이 이 축이 피해야 할 첫 번째 일이다.
    if arch.phase_cycle.remembers_in_verdict:

        @tool(description=load_tool_description("report_step_memory").body)
        async def report_step(
            step: int,
            passed: bool,
            message: str,
            thought: str,
            used_knowledge_ids: list[str] = [],
            capability_key: str | None = None,
            learned: str | None = None,
        ) -> str:
            # What the agent reads is `qa_run/<version>/tool_report_step_memory.md`.
            return await _record_verdict(
                step,
                passed,
                message,
                used_knowledge_ids,
                capability_key=capability_key,
                learned=learned,
                asks_memory=True,
            )

    else:

        @tool(description=load_tool_description("report_step").body)
        async def report_step(
            step: int,
            passed: bool,
            message: str,
            thought: str,
            used_knowledge_ids: list[str] = [],
        ) -> str:
            # What the agent reads is `qa_run/<version>/tool_report_step.md`.
            return await _record_verdict(step, passed, message, used_knowledge_ids)


    @tool(
        description=load_tool_description("report_issue").body.format(
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

    @tool(description=load_tool_description("finish_run").body)
    async def finish_run(passed: bool, summary: str, thought: str) -> str:
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

    @tool(description=load_tool_description("reply_to_operator").body)
    async def reply_to_operator(message: str, thought: str, step: int | None = None) -> str:
        await channel.say(message, step)
        return _answer("Sent.", channel.drain_operator_messages())

    return [
        wait_for_operator,
        report_step,
        report_issue,
        finish_run,
        reply_to_operator,
    ]
