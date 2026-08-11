"""The tools the QA agent drives the game with.

Each one is a round trip the agent chooses to make, which is what separates this
from the old design: the run advances because the agent asked, not because the
game happened to send something.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.arch import ResolvedArch, default_resolved_arch
from app.agents.qa.knowledge import (
    EXPAND_KNOWLEDGE_DESCRIPTION,
    FORGET_KNOWLEDGE_DESCRIPTION,
    KNOWLEDGE_RELATIONS,
    KNOWLEDGE_TAGS,
    LINK_KNOWLEDGE_DESCRIPTION,
    MAX_EXPAND_DEPTH,
    RECORD_KNOWLEDGE_DESCRIPTION,
    RESULT_LIMIT,
    SEARCH_KNOWLEDGE_DESCRIPTION,
    SIMILAR_LABEL,
    UNLINK_KNOWLEDGE_DESCRIPTION,
    UPDATE_KNOWLEDGE_DESCRIPTION,
    render_entry_label,
    render_expansion,
    render_missing_knowledge_warning,
    render_results,
)
from app.qa.channel import (
    KnowledgeSearchFailed,
    QaCancelled,
    QaRunChannel,
    bounded_operator_wait,
    with_operator_messages,
)
from app.qa.envelope import (
    IssuePayload,
    IssueSeverity,
    JsonRpcAction,
    KnowledgeCreatePayload,
    KnowledgeDeletePayload,
    KnowledgeLinkPayload,
    KnowledgeUnlinkPayload,
    KnowledgeUpdatePayload,
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

    def __init__(
        self,
        total_steps: int,
        step_meta: list[tuple[int | None, bool]] | None = None,
    ) -> None:
        self.total_steps = total_steps
        # 스텝별 (case_id, is_verification). report_step이 각 판정에 이를 붙이고, TC 판정(파생)은
        # `case_units`가 이 표로 구간을 잘라 그 구간 검증 스텝의 판정으로 정한다. 비면 단일-계층
        # 폴백(case_id/is_verification 미상) — 구식 호출자·테스트 호환용.
        self.step_meta: list[tuple[int | None, bool]] = step_meta or []
        self.step_results: list[QaStepResult] = []
        self.finished = False
        # The observation the agent last saw, so the next look is a diff.
        self.watermark = 0
        # How many times `finish_run` was reached. The first attempt made with
        # steps still unreported is pushed back on; a second one closes the run
        # regardless, so a game that genuinely cannot go on is never trapped.
        self.finish_attempts = 0
        # Attempts, not successes. A game whose SDK does not know the capture action
        # refuses every one of them, and counting only what worked would leave that
        # loop unbounded — the cap has to bind on the failing case too.
        self.captures_attempted = 0
        # Attempts again, and for the same reason: a search Orchestration refuses
        # every time would otherwise be a loop with no bound on it.
        self.knowledge_searches_attempted = 0
        # Attempts for the knowledge writes too, and here there is no alternative:
        # nothing answers a write (see `QaRunChannel.write_knowledge`), so success
        # is not something this side could count even if it wanted to.
        self.knowledge_records_attempted = 0
        self.knowledge_updates_attempted = 0
        self.knowledge_forgets_attempted = 0
        # Attempts once more. A report the far side drops still cost the run a
        # call, and a cap that only counted accepted ones would not bound the
        # loop where every report is being rejected.
        self.issues_attempted = 0
        # What `search_knowledge` has actually shown the agent, id -> summary.
        # A correction or a deletion may only name an id from here. An agent free
        # to pass any id could rewrite or erase an entry it never read, and on the
        # far side that id resolves to a real row — Orchestration cannot tell the
        # difference, so the check only exists if it exists here.
        self.knowledge_seen: dict[str, str] = {}
        # Entries this run deleted and has not written a replacement for, as
        # printable labels, oldest first.
        #
        # `update_knowledge` is what correcting an entry should be, but a run may
        # still delete and then record, and this is the only thing that knows one
        # is halfway through doing so. It is what lets a failing `record_knowledge`
        # say what is missing instead of reporting a bare failure, and what exempts
        # a replacement write from the write cap.
        self.knowledge_deleted_unreplaced: list[str] = []
        # Entries shown only as a one-line neighbour, id -> summary. NEVER popped.
        #
        # Kept apart from `knowledge_seen` because the two license different
        # things. `seen` means read in full and is what `update_knowledge` and
        # `forget_knowledge` require — deletion is the most destructive thing the
        # agent does, and a 120-character line is not having read the entry.
        # `glimpsed` is enough to assert a relation or to expand from, neither of
        # which destroys anything and both of which a summary can justify.
        self.knowledge_glimpsed: dict[str, str] = {}
        self.knowledge_links_attempted = 0
        self.knowledge_unlinks_attempted = 0
        self.knowledge_expands_attempted = 0
        # Handed to the vision middleware on the next model call. The tool cannot
        # return the image itself — an image block on a tool result is rejected by
        # the chat/completions API every model here is reached through.
        self._pending_captures: list[PendingCapture] = []
        # Set by `compact_context`, read and cleared by the compaction middleware
        # before the next model call. A flag rather than the tool doing the work
        # itself: the tool runs inside the tools node, where its own AIMessage is
        # already in state, and rewriting the message list from there would strand
        # that call without its result. See `app/agents/qa/compaction.py`.
        self.compaction_requested = False
        # How many compactions this run has been through. Read by the thrash guard
        # and reported at the end of the run.
        self.compactions = 0

    def remember_glimpsed(self, neighbours) -> None:
        """Record neighbour lines the agent was shown.

        Without this the agent is printed ids it then cannot use at all — every
        graph tool refuses an endpoint the run has not been shown. An id-less
        neighbour is skipped rather than stored under the empty string, for the
        same reason an id-less hit is.
        """
        for neighbour in neighbours:
            if neighbour.id:
                self.knowledge_glimpsed[neighbour.id] = neighbour.summary

    def knows_of(self, knowledge_id: str) -> bool:
        """Whether this run has been shown this entry at all, in full or as a line."""
        return knowledge_id in self.knowledge_seen or knowledge_id in self.knowledge_glimpsed

    @property
    def knowledge_writes_attempted(self) -> int:
        """Records and corrections against one allowance.

        Capped together because they fail together: either one spends the run's
        steps putting content into the knowledge base instead of reaching a
        verdict, which is the whole reason `max_records_per_run` exists.
        """
        return self.knowledge_records_attempted + self.knowledge_updates_attempted

    def unreported_steps(self) -> list[int]:
        """Scenario steps with no verdict yet, in order.

        Lives here rather than in the ledger that prints it because it is derived
        from `step_results`, and a second derivation elsewhere is how the ledger
        and the run come to disagree about what is left.
        """
        reported = {result.step for result in self.step_results}
        return [step for step in range(1, self.total_steps + 1) if step not in reported]

    def case_units(self) -> list[dict]:
        """연속 동일 case_id = 한 TC 구간. **TC 판정 = 그 구간의 검증(마지막) 스텝 판정**(2단 판정).

        `step_meta`(권위)로 구간을 자르고, 각 구간의 검증 스텝 판정을 `step_results`에서 찾아
        TC의 passed/message로 삼는다. 중간 스텝의 성공/실패는 각 스텝에 그대로 남아있고(steps),
        TC 판정과 별개다 — 중간이 실패해도 검증이 통과하면 TC는 통과다.
        """
        by_step = {result.step: result for result in self.step_results}
        units: list[dict] = []
        prev_cid: int | None = None
        for index, (cid, is_verification) in enumerate(self.step_meta, start=1):
            if cid is None:
                prev_cid = None
                continue
            if not units or cid != prev_cid:
                units.append(
                    {"case_no": len(units) + 1, "case_id": cid, "steps": [], "verify_step": None}
                )
            unit = units[-1]
            unit["steps"].append(index)
            if is_verification:
                unit["verify_step"] = index
            prev_cid = cid
        for unit in units:
            # 검증 스텝(없으면 구간 마지막 스텝)의 판정이 곧 TC 판정.
            decisive = unit["verify_step"] or (unit["steps"][-1] if unit["steps"] else None)
            result = by_step.get(decisive) if decisive else None
            unit["passed"] = bool(result and result.passed)
            unit["message"] = result.message if result else ""
        return units

    def build_summary(self) -> dict:
        """종단 STATUS에 싣는 2단 요약. steps[]가 원천, cases[]는 파생(구간 검증 스텝).

        finish_run(정상 종료)과 service._send_terminal(중단/실패)이 같은 형태를 내도록 한 곳에서
        만든다 — 두 경로가 다른 요약을 내면 다운스트림이 종료 사유마다 다른 스키마를 보게 된다.
        """
        total = self.total_steps
        steps_passed = sum(1 for result in self.step_results if result.passed)
        cases = self.case_units()
        cases_passed = sum(1 for unit in cases if unit["passed"])
        return {
            "steps": {
                "total": total,
                "passed": steps_passed,
                "failed": total - steps_passed,
                "items": [result.model_dump() for result in self.step_results],
            },
            "cases": {
                "total": len(cases),
                "passed": cases_passed,
                "failed": len(cases) - cases_passed,
                "items": cases,
            },
        }

    def add_pending_capture(self, capture: PendingCapture) -> None:
        self._pending_captures.append(capture)

    def take_pending_captures(self) -> list[PendingCapture]:
        pending = self._pending_captures
        self._pending_captures = []
        return pending


# The one description written out here rather than left as a docstring, because
# it has to name the cap and a docstring cannot interpolate one. An agent told
# only "screenshots are limited" spends them at the first opportunity; the number
# is what makes the budget something it can actually ration.
CAPTURE_SCREEN_DESCRIPTION = """Look at the actual picture on screen, not just the scene text.

Use this when the step is about how something *looks*: a layout that may be
broken, a button that may be covered by other UI, a sprite in the wrong state,
text that may be unreadable. The scene listing cannot express any of that.

Leave `target_id` out for the whole screen. Give one to see just that element's
area, at higher detail.

You get {limit} screenshots for the whole run and no more, so spend them on the
steps where looking is what decides the verdict.

The picture arrives right after this result, as its own message."""


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


def build_tools(
    channel: QaRunChannel, state: QaRunState, arch: ResolvedArch | None = None
) -> list[BaseTool]:
    arch = arch or default_resolved_arch()
    @tool
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

    @tool(description=CAPTURE_SCREEN_DESCRIPTION.format(limit=arch.max_captures_per_run))
    async def capture_screen(step: int, thought: str, target_id: int | None = None) -> str:
        # What the agent reads is CAPTURE_SCREEN_DESCRIPTION above, not this.
        # Returns the capture as a promise: the image itself is handed to the
        # vision middleware and arrives on the next model call.
        if state.captures_attempted >= arch.max_captures_per_run:
            # Refused with the reason, not silently: a run that keeps looking
            # instead of deciding will reach the deadline with nothing reported.
            return (
                f"You have used all {arch.max_captures_per_run} screenshots for this run. "
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

    @tool(
        description=SEARCH_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_searches_per_run, tags=", ".join(KNOWLEDGE_TAGS)
        )
    )
    async def search_knowledge(
        step: int, thought: str, query: str, tag: str | None = None
    ) -> str:
        # What the agent reads is SEARCH_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Deliberately not routed through `_run`: that path dispatches actions and
        # appends the scene they produced. A search moves nothing on screen, so a
        # scene view here would be the same picture the agent already has, paid for
        # again in context — exactly what `app/agents/qa/context.py` exists to stop.
        if state.knowledge_searches_attempted >= arch.max_searches_per_run:
            return (
                f"You have used all {arch.max_searches_per_run} knowledge searches for "
                "this run. Judge the remaining steps from the scenario and what you "
                "can see."
            )

        topic = (tag or "").strip().upper()
        if topic and topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out. Orchestration rejects an unknown tag
            # outright rather than ignoring it, so sending one would spend a round
            # trip and a search out of the run's budget to be told something this
            # side already knew.
            return (
                f"{tag!r} is not a knowledge topic, so the search was not sent. "
                f"Use one of {', '.join(KNOWLEDGE_TAGS)}, or leave `tag` out."
            )

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_searches_attempted += 1
        answer = await channel.search_knowledge(query, topic or None, RESULT_LIMIT)
        messages = channel.drain_operator_messages()
        remaining = arch.max_searches_per_run - state.knowledge_searches_attempted

        if answer is None:
            return with_operator_messages(
                "The knowledge base did not answer in time. Judge this step from "
                f"the scenario and what you can see. {remaining} search(es) left.",
                messages,
            )
        if isinstance(answer, KnowledgeSearchFailed):
            # Said plainly, with what to do instead. A failed lookup is a side
            # errand that failed, not a failed step — an agent told only that
            # something went wrong has been known to fail the step over it.
            return with_operator_messages(
                f"The knowledge search could not run — {answer.reason}. This says "
                "nothing about the game; judge this step from the scenario and what "
                f"you can see. {remaining} search(es) left.",
                messages,
            )
        # Remembered before the result is rendered, because this is what makes an
        # entry writable at all: `update_knowledge` and `forget_knowledge` both
        # refuse an id that never came back from a search in this run. An id-less
        # hit is skipped rather than stored under the empty string — it is not
        # addressable, so it can be neither corrected nor deleted.
        for entry in answer.results:
            if entry.id:
                state.knowledge_seen[entry.id] = entry.summary
            # Neighbours are recorded SEPARATELY, and the difference is the point.
            # `knowledge_seen` means "read in full", and that is the precondition
            # `update_knowledge` and `forget_knowledge` rest on; a clipped one-line
            # summary is not having read something. Putting neighbours in here
            # would let a run delete an entry it has only glimpsed, which is the
            # first regression this feature could ship.
            state.remember_glimpsed(entry.neighbors)
        return with_operator_messages(render_results(answer, remaining), messages)

    @tool(
        description=RECORD_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_records_per_run,
            tags=", ".join(KNOWLEDGE_TAGS),
            ui_tag="UI",
        )
    )
    async def record_knowledge(
        step: int, thought: str, tag: str, summary: str, description: str
    ) -> str:
        # What the agent reads is RECORD_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Not routed through `_run`, for the same reason `search_knowledge` is not:
        # nothing here touches the game, so a scene view on the result would be the
        # picture the agent already has, paid for again in context (ARTEL-180).
        #
        # Every refusal below carries `render_missing_knowledge_warning`. This tool
        # is the second half of a repair as often as it is a first write, and a
        # refusal phrased only as "nothing was recorded" reads as harmless in the
        # one case where it is not.
        outstanding = state.knowledge_deleted_unreplaced

        # The cap does not bind a replacement write. It exists to stop a run
        # narrating into the knowledge base; applied to the second half of a
        # repair it would make the budget itself the thing that loses knowledge.
        #
        # Corrections count against the same allowance — see
        # `QaRunState.knowledge_writes_attempted`.
        if state.knowledge_writes_attempted >= arch.max_records_per_run and not outstanding:
            return (
                f"You have used all {arch.max_records_per_run} knowledge writes for this "
                "run, so nothing was recorded. Carry on with the run and judge the "
                "remaining steps."
            )

        topic = (tag or "").strip().upper()
        if topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out, as with a search's tag. Orchestration
            # rejects an unknown topic, and its rejection never comes back down
            # this socket — so a frame sent anyway would leave the run believing it
            # had written something.
            return (
                f"{tag!r} is not a knowledge topic, so nothing was recorded. Use one "
                f"of {', '.join(KNOWLEDGE_TAGS)} and call this again."
            ) + render_missing_knowledge_warning(outstanding)

        fact = summary.strip()
        detail = description.strip()
        if not fact or not detail:
            # Same reason as the tag: Orchestration rejects a blank one on arrival
            # and says so only on its own timeline.
            return (
                "`summary` and `description` must both say something, so nothing was "
                "recorded. Write them out and call this again."
            ) + render_missing_knowledge_warning(outstanding)

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_records_attempted += 1
        try:
            await channel.write_knowledge(
                MessageType.KNOWLEDGE_CREATE,
                KnowledgeCreatePayload(tag=topic, summary=fact, description=detail),
            )
        except QaCancelled:
            # The operator ended the run. That is not this tool's to swallow.
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Storing knowledge is a side errand to the verdict, so a failed write
            # is reported and the run goes on. It is still stated plainly: an agent
            # told nothing would move on believing the fact was filed.
            return (
                f"The knowledge write could not be sent — {error}. Nothing was recorded."
            ) + render_missing_knowledge_warning(outstanding)

        replaced = bool(outstanding)
        state.knowledge_deleted_unreplaced = []
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_records_per_run - state.knowledge_writes_attempted, 0)

        lines = [f'Sent to the knowledge base, filed under {topic}: "{fact}".']
        if replaced:
            lines.append(
                "That completes the correction — the entry you deleted has been "
                "replaced, and nothing is outstanding. Next time use "
                "`update_knowledge`: it repairs an entry in one call, and the "
                "replacement keeps the original's id."
            )
        lines.append(
            "Nothing answers a knowledge write, so treat this as done and do not "
            f"send the same fact again in this run. {remaining} knowledge write(s) "
            "left."
        )
        return with_operator_messages("\n\n".join(lines), messages)

    @tool(
        description=UPDATE_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_records_per_run, tags=", ".join(KNOWLEDGE_TAGS)
        )
    )
    async def update_knowledge(
        step: int,
        thought: str,
        knowledge_id: str,
        tag: str | None = None,
        summary: str | None = None,
        description: str | None = None,
    ) -> str:
        # What the agent reads is UPDATE_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view, for the reason given on `record_knowledge` (ARTEL-180),
        # and nothing awaited: like the other two writes this is one-way, and a
        # tool that waited for `routeKnowledgeMutation` to answer would hang to its
        # timeout on every call, including the ones that worked.
        #
        # The budget is `max_records_per_run`, shared with `record_knowledge`
        # rather than counted apart, because both fail the run the same way — see
        # `QaRunState.knowledge_writes_attempted`. The constraint that a repair must
        # never be left half done by the budget still holds, from both ends: a
        # refused correction changes nothing, since it is one call and the entry is
        # untouched, and a delete-then-record still has its own exemption above.
        outstanding = state.knowledge_deleted_unreplaced

        def refused(reason: str) -> str:
            """A refusal, with whatever the run still owes appended to it.

            The rule `record_knowledge`'s refusals follow, and it applies here for
            a reason particular to this tool: `record_knowledge` is exempt from the
            cap while a deletion is outstanding, so a budget refusal from HERE is
            the only one a run can meet in the middle of a delete-then-record
            repair. Phrased as a bare "nothing was changed" it would read as
            harmless in exactly the state where it is not.
            """
            return reason + render_missing_knowledge_warning(outstanding)

        if state.knowledge_writes_attempted >= arch.max_records_per_run:
            return refused(
                f"You have used all {arch.max_records_per_run} knowledge writes for "
                "this run, so nothing was changed and the entry stands as it was."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The same guard `forget_knowledge` makes, for the same reason: on the
            # far side this id resolves to a real row, and nothing there can tell
            # that the agent never read it. An entry already deleted in this run is
            # gone from `knowledge_seen` too, so a correction cannot resurrect one.
            if target in state.knowledge_glimpsed:
                # Named as a neighbour line but never read in full. Said apart from
                # the case below because otherwise the agent meets a refusal it
                # cannot explain — it can see the id right there in the transcript.
                return refused(
                    f"Nothing was changed: you have seen {knowledge_id!r} only as a "
                    "neighbour line, which is a clipped summary rather than the "
                    "entry. Search for it so you read it in full, then correct it."
                )
            return refused(
                f"Nothing was changed: {knowledge_id!r} is not an entry "
                "`search_knowledge` returned in this run, and you can only correct "
                "what you have read. Search for it first and use the id printed "
                "with the hit."
            )

        # `None` and `""` are different requests and are kept apart all the way
        # down: an omitted field is left alone on the far side, a field sent blank
        # is rejected there. So a blank tag falls into the refusal below rather
        # than being read as "leave the topic alone" — the two spellings must not
        # quietly mean the same thing when the message here says they do not.
        topic = tag.strip().upper() if tag is not None else None
        if topic is not None and topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out, as on a record. Orchestration rejects an
            # unknown topic and says so only on its own timeline, so a frame sent
            # anyway would leave the run believing the entry had been corrected.
            return refused(
                f"{tag!r} is not a knowledge topic, so nothing was changed. Use one "
                f"of {', '.join(KNOWLEDGE_TAGS)}, or leave `tag` out to keep the "
                "topic it already has."
            )

        fact = summary.strip() if summary is not None else None
        detail = description.strip() if description is not None else None
        if (summary is not None and not fact) or (description is not None and not detail):
            return refused(
                "Nothing was changed: `summary` and `description` must say something "
                "when you send them. Leave a field out entirely to keep what the "
                "entry already has, and call this again."
            )
        if topic is None and fact is None and detail is None:
            return refused(
                "Nothing was changed: a correction has to carry at least one of "
                "`tag`, `summary` or `description`. Say what the entry should now "
                "be, or use `forget_knowledge` if it should simply be gone."
            )

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_updates_attempted += 1
        try:
            await channel.write_knowledge(
                MessageType.KNOWLEDGE_UPDATE,
                KnowledgeUpdatePayload(
                    knowledge_id=target, tag=topic, summary=fact, description=detail
                ),
            )
        except QaCancelled:
            # The operator ended the run. That is not this tool's to swallow.
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Nothing left the socket, so this entry is exactly as it was. Said
            # plainly all the same: an agent told only that something failed
            # carries on believing the entry is now right. A deletion still owed
            # from earlier is named too, for the reason `refused` gives.
            return refused(
                f"The correction could not be sent — {error}. Nothing was changed "
                "and the entry is still on file exactly as it was."
            )

        # Still an entry this run has read, so it stays correctable and deletable —
        # a correction is not a reason to forget having seen it. The stored summary
        # follows the correction because it is what every later label prints: left
        # alone, a `forget_knowledge` after this would name the sentence the agent
        # has just replaced.
        state.knowledge_seen[target] = (
            fact if fact is not None else state.knowledge_seen[target]
        )
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_records_per_run - state.knowledge_writes_attempted, 0)

        changed = ", ".join(
            name
            for name, value in (("tag", topic), ("summary", fact), ("description", detail))
            if value is not None
        )
        # Labelled with the summary the entry now has, not the one it had. Every
        # other write result echoes what was sent, and a sentence quoted right
        # after the word "Corrected" is read as the entry's current text — printing
        # the replaced one here would teach the run the correction had not landed.
        return with_operator_messages(
            f"Corrected {render_entry_label(target, state.knowledge_seen[target])}. "
            f"Sent: {changed}; the rest of the entry is left as it was. It keeps "
            "its id, so this stays readable as a repair rather than as a deletion "
            "and a new entry.\n\n"
            "Nothing answers a knowledge write, so treat this as done and do not "
            f"send the same correction again in this run. {remaining} knowledge "
            "write(s) left.",
            messages,
        )

    @tool(description=FORGET_KNOWLEDGE_DESCRIPTION.format(limit=arch.max_forgets_per_run))
    async def forget_knowledge(step: int, thought: str, knowledge_id: str) -> str:
        # What the agent reads is FORGET_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view here either, for the reason given on `record_knowledge`.
        if state.knowledge_forgets_attempted >= arch.max_forgets_per_run:
            return (
                f"You have used all {arch.max_forgets_per_run} knowledge deletion(s) for "
                "this run, so nothing was deleted. If another entry still looks "
                "wrong, say so in `report_step` instead of deleting it."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The whole guard against deleting blind. Orchestration resolves this id
            # to a real row and has no way to know the agent never read it, so this
            # check exists here or nowhere. An id already deleted in this run is
            # gone from `knowledge_seen` too, which is what stops a second delete.
            if target in state.knowledge_glimpsed:
                return (
                    f"Nothing was deleted: you have seen {knowledge_id!r} only as a "
                    "neighbour line, which is a clipped summary rather than the "
                    "entry. Deleting on that is exactly what this guard is for — "
                    "search for it, read it in full, and decide then."
                )
            return (
                f"Nothing was deleted: {knowledge_id!r} is not an entry "
                "`search_knowledge` returned in this run, and you can only delete "
                "what you have read. Search for it first and use the id printed with "
                "the hit."
            )

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_forgets_attempted += 1
        try:
            await channel.write_knowledge(
                MessageType.KNOWLEDGE_DELETE, KnowledgeDeletePayload(knowledge_id=target)
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            # Nothing went out, so nothing was deleted and nothing is outstanding.
            # The entry stays in `knowledge_seen`, which leaves it retryable.
            return (
                f"The deletion could not be sent — {error}. Nothing was deleted and "
                "the entry is still on file."
            )

        # Taken out of what may be deleted and recorded as outstanding, in that
        # order, before the result is composed: from here on a `record_knowledge`
        # that fails is able to name exactly what is missing.
        label = render_entry_label(target, state.knowledge_seen.pop(target, ""))
        state.knowledge_deleted_unreplaced.append(label)
        messages = channel.drain_operator_messages()
        remaining = max(arch.max_forgets_per_run - state.knowledge_forgets_attempted, 0)

        return with_operator_messages(
            f"Deleted {label}. This cannot be undone from here.\n\n"
            "If you deleted it in order to CORRECT it, that was `update_knowledge`, "
            "and what you have now is half a repair: call `record_knowledge` NOW "
            "with the corrected version, before anything else, or this run has "
            "removed the knowledge rather than fixed it.\n\n"
            f"{remaining} deletion(s) left in this run.",
            messages,
        )

    @tool(
        description=LINK_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_links_per_run, relations=", ".join(KNOWLEDGE_RELATIONS)
        )
    )
    async def link_knowledge(
        step: int,
        thought: str,
        from_knowledge_id: str,
        to_knowledge_id: str,
        relation: str,
        note: str,
    ) -> str:
        # What the agent reads is LINK_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Not routed through `_run`, for the same reason the other knowledge tools
        # are not: nothing here touches the game.
        #
        # EVERY check below happens before the frame goes out, and that is not
        # defensiveness. A link is one-way — Orchestration's rejection becomes a
        # row on the operator's timeline and never comes back down this socket — so
        # a frame that should not have been sent is reported to the model as a
        # success while nothing was written.
        if state.knowledge_links_attempted >= arch.max_links_per_run:
            return (
                f"You have used all {arch.max_links_per_run} knowledge links for this "
                "run, so nothing was linked. Spend the rest of the run judging steps."
            )

        kind = (relation or "").strip().upper()
        if kind not in KNOWLEDGE_RELATIONS:
            return (
                f"{relation!r} is not a knowledge relation, so nothing was sent. "
                f"Use one of {', '.join(KNOWLEDGE_RELATIONS)} — and if none of them "
                "fits, do not link these two at all."
            )

        reason = (note or "").strip()
        if not reason:
            # The far side stores `note` NOT NULL and would drop this frame in
            # silence. Refused here so the agent learns the link did not happen.
            return (
                "Nothing was linked: `note` is required. It is the only record of "
                "why you thought the connection was real — and for LEADS_TO it is "
                "what you did to get there, which is what makes the route usable."
            )

        source = (from_knowledge_id or "").strip()
        target = (to_knowledge_id or "").strip()
        if source == target:
            return "Nothing was linked: an entry cannot be related to itself."
        for endpoint in (source, target):
            if not state.knows_of(endpoint):
                return (
                    f"Nothing was linked: {endpoint!r} is not an entry this run has "
                    "been shown. Search for it first and use the id printed with the "
                    "hit or with a neighbour line."
                )

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_links_attempted += 1
        try:
            await channel.write_knowledge(
                MessageType.KNOWLEDGE_LINK,
                KnowledgeLinkPayload(
                    from_knowledge_id=source,
                    to_knowledge_id=target,
                    relation=kind,
                    note=reason,
                ),
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            return f"The link could not be sent — {error}. Nothing was linked."
        messages = channel.drain_operator_messages()
        remaining = arch.max_links_per_run - state.knowledge_links_attempted
        return with_operator_messages(
            f"Sent: {source} {kind.lower()} {target}. Nothing answers a link, so do "
            f"not send it again.\n\n{remaining} link(s) left in this run.",
            messages,
        )

    @tool(description=UNLINK_KNOWLEDGE_DESCRIPTION.format(limit=arch.max_unlinks_per_run))
    async def unlink_knowledge(
        step: int,
        thought: str,
        from_knowledge_id: str,
        to_knowledge_id: str,
        relation: str,
    ) -> str:
        # What the agent reads is UNLINK_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Validated locally for the same reason `link_knowledge` is: one-way frame,
        # so a rejection would be invisible to the model.
        if state.knowledge_unlinks_attempted >= arch.max_unlinks_per_run:
            return (
                f"You have used all {arch.max_unlinks_per_run} knowledge unlink(s) for "
                "this run, so nothing was removed. If another link still looks wrong, "
                "say so in `report_issue` instead."
            )

        kind = (relation or "").strip().upper()
        if kind not in KNOWLEDGE_RELATIONS:
            return (
                f"{relation!r} is not a knowledge relation, so nothing was sent. "
                f"Name the relation as it was printed to you, one of "
                f"{', '.join(KNOWLEDGE_RELATIONS)}."
            )

        source = (from_knowledge_id or "").strip()
        target = (to_knowledge_id or "").strip()
        for endpoint in (source, target):
            if not state.knows_of(endpoint):
                return (
                    f"Nothing was removed: {endpoint!r} is not an entry this run has "
                    "been shown, so you have not seen the link you are removing."
                )

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_unlinks_attempted += 1
        try:
            await channel.write_knowledge(
                MessageType.KNOWLEDGE_UNLINK,
                KnowledgeUnlinkPayload(
                    from_knowledge_id=source, to_knowledge_id=target, relation=kind
                ),
            )
        except QaCancelled:
            raise
        except Exception as error:  # noqa: BLE001 - a dead socket must not end the run here
            return f"The unlink could not be sent — {error}. Nothing was removed."
        messages = channel.drain_operator_messages()
        remaining = arch.max_unlinks_per_run - state.knowledge_unlinks_attempted
        return with_operator_messages(
            f"Sent: removing {source} {kind.lower()} {target}. Nothing answers an "
            f"unlink, so do not send it again.\n\n{remaining} unlink(s) left in this run.",
            messages,
        )

    @tool(
        description=EXPAND_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_expands_per_run,
            relations=", ".join(KNOWLEDGE_RELATIONS),
            similar=SIMILAR_LABEL,
        )
    )
    async def expand_knowledge(
        step: int, thought: str, knowledge_id: str, depth: int = 1
    ) -> str:
        # What the agent reads is EXPAND_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Three outcomes handled exactly as `search_knowledge` handles them, and
        # for the same reason: none of timeout, refusal or empty answer is a reason
        # to fail the step.
        if state.knowledge_expands_attempted >= arch.max_expands_per_run:
            return (
                f"You have used all {arch.max_expands_per_run} knowledge expansions "
                "for this run. The neighbours already printed with your searches are "
                "what you have."
            )

        target = (knowledge_id or "").strip()
        if not state.knows_of(target):
            return (
                f"{target!r} is not an entry this run has been shown, so nothing was "
                "expanded. Search for it first and use the id printed with the hit."
            )

        await channel.note(thought, LogCategory.THOUGHT, step)
        state.knowledge_expands_attempted += 1
        # Clamped here as well as on the far side. Orchestration clamps rather than
        # refuses, so this only saves a round trip — but it also keeps the number
        # the agent is told about and the number it gets from drifting apart.
        answer = await channel.expand_knowledge(
            target, max(1, min(depth, MAX_EXPAND_DEPTH)), include_similar=True
        )
        messages = channel.drain_operator_messages()
        remaining = arch.max_expands_per_run - state.knowledge_expands_attempted

        if answer is None:
            return with_operator_messages(
                "The knowledge base did not answer in time. Carry on with what you "
                f"already have. {remaining} expansion(s) left.",
                messages,
            )
        if isinstance(answer, KnowledgeSearchFailed):
            return with_operator_messages(
                f"The expansion could not run — {answer.reason}. This says nothing "
                f"about the game. {remaining} expansion(s) left.",
                messages,
            )
        # Same split as a search: what the expansion showed is glimpsed, not read.
        state.remember_glimpsed(answer.neighbors)
        return with_operator_messages(render_expansion(answer, remaining), messages)

    @tool
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

    @tool
    async def enter_text(step: int, target_id: int, value: str, thought: str) -> str:
        """Type into a text field. `target_id` must be an id from the current scene."""
        return await _run(
            [JsonRpcAction(id=1, method="enter_text", params=[target_id, value])],
            thought,
            f"Typing into {target_id}",
            step,
        )

    @tool
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

    @tool
    async def move_pointer(step: int, x: float, y: float, thought: str) -> str:
        """Move the pointer to a point on the screen, without pressing anything.

        `x` and `y` are screen pixels, taken from the scene exactly as printed:
        an element's `@ x,y` is its centre, and it belongs here unchanged — no
        conversion of any kind. Use this to hover, or to put the pointer
        somewhere a target id cannot address — a map, a canvas, an inventory slot.
        """
        return await _run(
            [JsonRpcAction(id=1, method="move_mouse", params=[x, y])],
            thought,
            f"Moving the pointer to ({x}, {y})",
            step,
        )

    @tool
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

    @tool
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

    @tool
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

    @tool
    async def set_input_axis(step: int, axis_name: str, value: float, thought: str) -> str:
        """Drive a named input axis, for a game that reads axes rather than keys.

        `axis_name` is a Unity Input Manager axis and is CASE SENSITIVE —
        "Horizontal", "Vertical", "Jump" are the stock ones. `value` runs from
        -1 to 1: 1 and -1 are the two directions, 0 is centred. A value outside
        that range is refused, and so is an axis the game has not set up, so a
        misspelled name comes back as an error rather than as silence.

        Use this when `hold_key` does nothing. A game that reads
        `Input.GetAxis("Horizontal")` cannot see a held key at all: the key tool
        reports success and the character does not move.

        Nothing centres this for you. Call it again with 0 before you judge the
        step, or every step after it runs with the axis pushed over.
        """
        return await _run(
            [JsonRpcAction(id=1, method="set_axis", params=[axis_name, value])],
            thought,
            f"Setting axis {axis_name} to {value}",
            step,
        )

    @tool
    async def set_input_button(step: int, axis_name: str, pressed: bool, thought: str) -> str:
        """Hold or release a named input button, for a game that reads buttons by name.

        In Unity a button IS an axis: "Jump" is an axis entry whose positive side
        is a key, and the game may read it with `GetButton("Jump")` instead of
        checking the key itself. `axis_name` is that name, CASE SENSITIVE, and an
        axis the game has not set up comes back as an error.

        `pressed=True` holds it, `pressed=False` lets go. Release is what reports
        the button coming up, so a game watching for that edge needs the second
        call and not a value of 0.

        Nothing releases this for you. Call it with `pressed=False` before you
        judge the step, or the game keeps seeing the button down for the rest of
        the run.
        """
        return await _run(
            [JsonRpcAction(id=1, method="set_button", params=[axis_name, pressed])],
            thought,
            f"{'Holding' if pressed else 'Releasing'} button {axis_name}",
            step,
        )

    @tool
    async def release_key(step: int, key_code: str, thought: str) -> str:
        """Let go of a key held by `hold_key`. `key_code` must be the same one."""
        return await _run(
            [JsonRpcAction(id=1, method="key_up", params=[key_code])],
            thought,
            f"Releasing {key_code}",
            step,
        )

    @tool
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

        Coordinates are screen pixels, taken from the scene unchanged, as with
        `move_pointer`. `button` is 0 for left, 1 for right, 2 for middle.

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

    @tool
    async def pause_game_time(step: int, thought: str) -> str:
        """Freeze game time, so the screen stops changing while you read it.

        Use this when the thing you have to judge does not stay still — a hit
        effect, a countdown, a toast that disappears, a cutscene that plays past
        the moment the step is about. Clicking, typing and observing all keep
        working while time is frozen, because they do not run on game time.

        Nothing unfreezes this for you. Call `resume_game_time` before you report
        the step, or every step after it runs against a stopped game.
        """
        return await _run(
            [JsonRpcAction(id=1, method="pause_time")],
            thought,
            "Pausing game time",
            step,
        )

    @tool
    async def resume_game_time(step: int, thought: str) -> str:
        """Let game time run again, at the speed it had before the pause.

        Fails if the game was not paused by `pause_game_time` — the speed a game
        chose for itself is not yours to overwrite.
        """
        return await _run(
            [JsonRpcAction(id=1, method="resume_time")],
            thought,
            "Resuming game time",
            step,
        )

    @tool
    async def reset_game(step: int, thought: str) -> str:
        """Put the game back to the state the run started in.

        For a step that needs a clean game and no path back to one — a tutorial
        that plays once a session, a level already cleared, a wrong branch taken
        three screens ago. Cheaper than asking the operator to restart, and it
        keeps the run alive.

        It reloads the game's first scene, so everything on screen now is gone,
        and so is whatever the game was keeping across scene loads: managers,
        score, inventory. What it cannot undo is saved data — a game that writes
        progress to disk comes back holding it, and a step that depends on a
        fresh save file needs the operator.

        A `pause_game_time` freeze and any held key or mouse button are released
        first, so the fresh game starts with nothing pressed. Every target id you
        have is dead afterwards; observe before you act again.
        """
        return await _run(
            [JsonRpcAction(id=1, method="reset_game")],
            thought,
            "Resetting the game",
            step,
        )

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
        await channel.note(thought, LogCategory.THOUGHT, step)
        waited = bounded_operator_wait(timeout_seconds)
        messages = await channel.wait_for_operator(timeout_seconds)
        if not messages:
            return (
                f"The operator said nothing within {waited:g}s. Decide for "
                "yourself whether to wait again, carry on with what you have, or "
                "judge the step failed."
            )
        return with_operator_messages("The operator answered.", messages)

    @tool
    async def report_step(step: int, passed: bool, message: str, thought: str) -> str:
        """Record the verdict for one scenario step, with the evidence for it.

        Call this once per step, right after you have observed the result of that
        step's action. For a step that verifies an expected result, `passed` is
        whether that result occurred (this is also its test case's verdict); for
        any other step, `passed` is whether you carried the action out. `message`
        should cite what you saw, and `thought` is how you reached the verdict.
        """
        await channel.note(thought, LogCategory.THOUGHT, step)
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
        remaining = state.total_steps - len(state.step_results)
        if remaining <= 0:
            return "Recorded. That was the last step — finish the run."
        # The verdict is recorded either way; what differs is the pull to keep
        # going. A failure is where the loop is most tempted to call it a day, so
        # that is where the next move has to be spelled out rather than implied.
        body = f"Recorded. {remaining} step(s) left — continue with step {step + 1}."
        if passed:
            return body
        return f"{body} A failed step is not a reason to stop."

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
        await channel.note(thought, LogCategory.THOUGHT, step)
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
        return f"Filed as {checked.value}. {remaining} issue(s) left this run."

    @tool
    async def finish_run(passed: bool, summary: str, thought: str) -> str:
        """End the run. Call this once, after the last step has been reported.

        A step with no verdict yet is worth attempting before you close: the run
        was opened to find out about all of them. Calling this with steps still
        unreported sends you back to them once.

        `thought` is how you reached the overall verdict; it goes on the timeline.
        """
        await channel.note(thought, LogCategory.THOUGHT)
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
        await channel.note(thought, LogCategory.THOUGHT, step)
        await channel.say(message, step)
        return "Sent."

    # Each name below is the decorated tool, not the function: `@tool` takes the
    # name off the function itself, so a tool can no longer end up filed under a
    # name string that drifted away from what it is called here.
    tools: list[BaseTool] = [
        observe_scene,
        search_knowledge,
        record_knowledge,
        update_knowledge,
        forget_knowledge,
        link_knowledge,
        unlink_knowledge,
        expand_knowledge,
        click_button,
        enter_text,
        press_key,
        move_pointer,
        hold_mouse_button,
        release_mouse_button,
        hold_key,
        release_key,
        set_input_axis,
        set_input_button,
        drag_pointer,
        pause_game_time,
        resume_game_time,
        reset_game,
        wait_for_operator,
        report_step,
        report_issue,
        finish_run,
        reply_to_operator,
    ]

    # A run without vision is not offered the tool at all. Left in, it would be
    # called, cost a game round trip, and produce an image nothing can look at —
    # and the agent would have no way to know why looking did not help. This is
    # also why the tool set is part of the arch fingerprint: a run with the tool
    # and a run without it are two different agents, not one agent configured.
    if arch.vision:
        tools.append(capture_screen)
    return tools
