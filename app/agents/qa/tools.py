"""The tools the QA agent drives the game with.

Each one is a round trip the agent chooses to make, which is what separates this
from the old design: the run advances because the agent asked, not because the
game happened to send something.
"""

from dataclasses import dataclass
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.knowledge import (
    FORGET_KNOWLEDGE_DESCRIPTION,
    KNOWLEDGE_TAGS,
    MAX_FORGETS_PER_RUN,
    MAX_RECORDS_PER_RUN,
    MAX_SEARCHES_PER_RUN,
    RECORD_KNOWLEDGE_DESCRIPTION,
    RESULT_LIMIT,
    SEARCH_KNOWLEDGE_DESCRIPTION,
    render_entry_label,
    render_missing_knowledge_warning,
    render_results,
)
from app.agents.qa.vision import MAX_CAPTURES_PER_RUN
from app.qa.channel import (
    KnowledgeSearchFailed,
    QaCancelled,
    QaRunChannel,
    bounded_operator_wait,
    with_operator_messages,
)
from app.qa.envelope import (
    JsonRpcAction,
    KnowledgeCreatePayload,
    KnowledgeDeletePayload,
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
        # Attempts again, and for the same reason: a search Orchestration refuses
        # every time would otherwise be a loop with no bound on it.
        self.knowledge_searches_attempted = 0
        # Attempts for the knowledge writes too, and here there is no alternative:
        # nothing answers a write (see `QaRunChannel.write_knowledge`), so success
        # is not something this side could count even if it wanted to.
        self.knowledge_records_attempted = 0
        self.knowledge_forgets_attempted = 0
        # What `search_knowledge` has actually shown the agent, id -> summary.
        # A deletion may only name an id from here. An agent free to pass any id
        # could erase an entry it never read, and on the far side that id resolves
        # to a real row — Orchestration cannot tell the difference, so the check
        # only exists if it exists here.
        self.knowledge_seen: dict[str, str] = {}
        # Entries this run deleted and has not written a replacement for, as
        # printable labels, oldest first.
        #
        # There is no update tool, so correcting an entry is a delete followed by a
        # record, and this is the only thing that knows a run is halfway through
        # one. It is what lets a failing `record_knowledge` say what is missing
        # instead of reporting a bare failure, and what exempts a replacement write
        # from the record cap.
        self.knowledge_deleted_unreplaced: list[str] = []
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


def build_tools(
    channel: QaRunChannel, state: QaRunState, supports_vision: bool = True
) -> list[BaseTool]:
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

    @tool(description=CAPTURE_SCREEN_DESCRIPTION.format(limit=MAX_CAPTURES_PER_RUN))
    async def capture_screen(step: int, thought: str, target_id: int | None = None) -> str:
        # What the agent reads is CAPTURE_SCREEN_DESCRIPTION above, not this.
        # Returns the capture as a promise: the image itself is handed to the
        # vision middleware and arrives on the next model call.
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

    @tool(
        description=SEARCH_KNOWLEDGE_DESCRIPTION.format(
            limit=MAX_SEARCHES_PER_RUN, tags=", ".join(KNOWLEDGE_TAGS)
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
        if state.knowledge_searches_attempted >= MAX_SEARCHES_PER_RUN:
            return (
                f"You have used all {MAX_SEARCHES_PER_RUN} knowledge searches for "
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
        remaining = MAX_SEARCHES_PER_RUN - state.knowledge_searches_attempted

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
        # entry deletable: `forget_knowledge` refuses an id that never came back
        # from a search in this run. An id-less hit is skipped rather than stored
        # under the empty string — it is not addressable, so it is not deletable.
        for entry in answer.results:
            if entry.id:
                state.knowledge_seen[entry.id] = entry.summary
        return with_operator_messages(render_results(answer, remaining), messages)

    @tool(
        description=RECORD_KNOWLEDGE_DESCRIPTION.format(
            limit=MAX_RECORDS_PER_RUN, tags=", ".join(KNOWLEDGE_TAGS)
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
        if state.knowledge_records_attempted >= MAX_RECORDS_PER_RUN and not outstanding:
            return (
                f"You have used all {MAX_RECORDS_PER_RUN} knowledge records for this "
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
        remaining = max(MAX_RECORDS_PER_RUN - state.knowledge_records_attempted, 0)

        lines = [f'Sent to the knowledge base, filed under {topic}: "{fact}".']
        if replaced:
            lines.append(
                "That completes the correction — the entry you deleted has been "
                "replaced, and nothing is outstanding."
            )
        lines.append(
            "Nothing answers a knowledge write, so treat this as done and do not "
            f"send the same fact again in this run. {remaining} record(s) left."
        )
        return with_operator_messages("\n\n".join(lines), messages)

    @tool(description=FORGET_KNOWLEDGE_DESCRIPTION.format(limit=MAX_FORGETS_PER_RUN))
    async def forget_knowledge(step: int, thought: str, knowledge_id: str) -> str:
        # What the agent reads is FORGET_KNOWLEDGE_DESCRIPTION, not this.
        #
        # No scene view here either, for the reason given on `record_knowledge`.
        if state.knowledge_forgets_attempted >= MAX_FORGETS_PER_RUN:
            return (
                f"You have used all {MAX_FORGETS_PER_RUN} knowledge deletion(s) for "
                "this run, so nothing was deleted. If another entry still looks "
                "wrong, say so in `report_step` instead of deleting it."
            )

        target = (knowledge_id or "").strip()
        if target not in state.knowledge_seen:
            # The whole guard against deleting blind. Orchestration resolves this id
            # to a real row and has no way to know the agent never read it, so this
            # check exists here or nowhere. An id already deleted in this run is
            # gone from `knowledge_seen` too, which is what stops a second delete.
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
        remaining = max(MAX_FORGETS_PER_RUN - state.knowledge_forgets_attempted, 0)

        return with_operator_messages(
            f"Deleted {label}. This cannot be undone from here.\n\n"
            "If you deleted it in order to CORRECT it, call `record_knowledge` NOW "
            "with the corrected version, before anything else. There is no update "
            "tool: that was half of a repair, and a run that stops here has removed "
            "the knowledge rather than fixed it.\n\n"
            f"{remaining} deletion(s) left in this run.",
            messages,
        )

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

    @tool
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
        forget_knowledge,
        click_button,
        enter_text,
        press_key,
        move_pointer,
        hold_mouse_button,
        release_mouse_button,
        hold_key,
        release_key,
        drag_pointer,
        pause_game_time,
        resume_game_time,
        wait_for_operator,
        report_step,
        finish_run,
        reply_to_operator,
    ]

    # A model that cannot read images is not offered the tool at all. Left in, it
    # would be called, cost a game round trip, and produce an image nothing can
    # look at — and the agent would have no way to know why looking did not help.
    if supports_vision:
        tools.append(capture_screen)
    return tools
