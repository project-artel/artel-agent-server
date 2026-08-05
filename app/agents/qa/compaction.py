"""Compacting the QA run's conversation when it outgrows the model.

`fold_stale_scenes` (`app/agents/qa/context.py`) holds down the biggest source of
growth, but not the rest of it: folded placeholders, action outcome lines, step
verdicts, knowledge search results and the model's own reasoning all stay in the
conversation for the whole run. A long scenario still reaches the provider's
limit, and it reaches it mid-run — with half the verdicts recorded and no way to
finish.

This replaces the old part of the conversation with a summary once it nears that
limit, and keeps the newest messages verbatim. Two things make that safe to do to
a QA run in particular:

- The summary is followed by a **progress ledger**, which is generated rather
  than summarized. Step verdicts, the steps still without one, and everything the
  operator has said are facts this process already holds; asking a model to
  remember them accurately would be trading certainty for nothing.
- The current screen needs no restating. `_append_current_scene` in
  `app/agents/qa/runner.py` puts `SceneMemory.render_now()` at the end of every
  model call, so the agent's view of the game survives compaction untouched.

Two triggers, one mechanism: the automatic one at a fraction of the model's input
budget, and `compact_context`, which the agent calls when it judges its own
history to have become unwieldy.
"""

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AnyMessage, BaseMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.tools import BaseTool, tool

from app.agents.qa.context import fold_stale_scenes
from app.agents.qa.tools import QaRunState
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory

# What `SummarizationMiddleware` returns as a summary when it could not produce
# one. It catches every exception out of the summarizing model and turns it into
# one of these strings, then hands the result back through the same path a real
# summary takes — so the conversation is wiped and replaced with the error. These
# prefixes are how that case is told apart from a summary; see `abefore_model`.
_SUMMARY_FAILURE_PREFIXES = (
    "Error generating summary:",
    "Previous conversation was too long",
)

# Set by LangChain on the messages it builds out of a summary. Used to find where
# the summary ends and the preserved tail begins, so the ledger can go between
# them. Semi-private, hence the positional fallback in `_insert_after_summary`.
_SUMMARY_SOURCE = "summarization"

# LangChain wraps the summary in a line of its own before putting it back in the
# conversation. Stripped so that what is checked for failure, and what reaches the
# log, is the summary itself rather than the sentence introducing it.
_SUMMARY_PREAMBLE = "Here is a summary of the conversation to date:"


QA_SUMMARY_PROMPT = """<role>
QA Run Context Extraction
</role>

<primary_objective>
You are compressing the working history of a QA agent that is executing an
approved test scenario against a live Unity game, step by step, through tools.
The history below will be REPLACED by what you write.
</primary_objective>

<what_you_do_not_need_to_carry>
Two things are restored separately and must not be re-derived here:

- The step verdicts, the steps still awaiting one, and everything the operator
  has said. These are restated verbatim from a record, immediately after your
  summary.
- The current state of the screen. It is attached fresh to every request.

Spend none of your words on either. Write what only the history knows.
</what_you_do_not_need_to_carry>

<instructions>
Structure the summary with the sections below. Each is a checklist: fill it, or
write "None" if there is genuinely nothing to report.

## SCENARIO

The scenario's title and what it is trying to establish, then its steps in order
with each one's intended action and expected result.

## WHAT HAS BEEN TRIED

Per step attempted: what was actually clicked, typed or pressed, on which
element, and what the game did in response. For anything that failed, say why, so
the same dead end is not walked into twice.

## GAME BEHAVIOUR LEARNED

What was discovered by doing rather than by being told: which screen leads where,
which key advances dialogue, what needs a wait before it is ready, which element
ids or labels turned out to mean what. This is the expensive knowledge in the
history — it cost tool calls to obtain and cannot be recovered by reasoning.

## OPEN PROBLEMS

Anything unresolved: a step abandoned part-way, a screen that would not respond,
a wait that has not paid off yet.

## NEXT ACTION

The single concrete thing to do next.
</instructions>

<constraints>
Never write that a step passed or failed. Verdicts are recorded elsewhere and
restated after your summary; one invented here would contradict the record.
Never invent an element id — only ids the history actually shows.
Write in English regardless of the language the run is reported in. This text is
read by a model, not by the operator.
</constraints>

Respond ONLY with the extracted context. No preamble, no closing remarks.

<messages>
Messages to summarize:
{messages}
</messages>"""


COMPACT_CONTEXT_DESCRIPTION = """Compress your own conversation history.

Reach for this when the history behind you has become unwieldy — many steps done,
long stretches of exploration, repeated looks at the same screen — and it is
getting in the way of deciding what to do next. It happens automatically when the
conversation approaches the model's limit; this is for when you would rather not
wait for that.

Everything that matters survives: the verdicts you have recorded, which steps
still need one, the step to do next, and anything the operator told you are all
restated to you immediately afterwards, and the current screen is attached to
every turn regardless. What you lose is the detail of how you got here.

Say what to do next in `reason` and then simply carry on — there is no need to
call this again or to re-check what you have already recorded."""


def build_compact_tool(state: QaRunState) -> BaseTool:
    """The agent's own handle on compaction.

    The tool only raises a flag. It cannot do the compacting itself: it runs in
    the tools node, where the `AIMessage` carrying this very call is already in
    the conversation, and rewriting the message list from here would delete that
    message while leaving this tool's result behind it — a tool result answering
    a call that no longer exists, which every provider rejects. Raising a flag
    lets the middleware do the work one step later, from the one place that
    already knows how to cut without splitting a call from its result.
    """

    @tool(description=COMPACT_CONTEXT_DESCRIPTION)
    async def compact_context(reason: str) -> str:
        state.compaction_requested = True
        return (
            "Compaction will run before your next turn. Your recorded verdicts, "
            "the steps still to do, and anything the operator told you are "
            "preserved and will be restated to you. Carry on from where you are."
        )

    return compact_context


def render_progress_ledger(state: QaRunState, channel: QaRunChannel) -> str:
    """What the run knows for certain, written out for the model to re-read.

    Generated, never summarized. Everything here is already held as data, and a
    summarizing model asked to carry it would sometimes carry it wrong — a
    misremembered verdict is worse than no summary at all, because the agent would
    then either redo a step it has already judged or skip one it has not.
    """
    lines = [
        "CONTEXT COMPACTED. The conversation above was summarized. This block is "
        "the authoritative record of the run so far — where it disagrees with the "
        "summary, this is right.",
        "",
        f"Scenario steps: {state.total_steps}",
    ]

    if state.step_results:
        lines.append("")
        lines.append("Verdicts already recorded — do NOT report or re-attempt these:")
        for result in state.step_results:
            verdict = "PASS" if result.passed else "FAIL"
            lines.append(f"  step {result.step}: {verdict} — {result.message}")
    else:
        lines.append("")
        lines.append("No step has a verdict yet.")

    remaining = state.unreported_steps()
    if remaining:
        listed = ", ".join(str(step) for step in remaining)
        lines.append("")
        lines.append(f"Steps still without a verdict: {listed}")
        lines.append(
            f"Continue with step {remaining[0]}. Observe the screen first if you "
            "are unsure where the game is."
        )
    else:
        lines.append("")
        lines.append("Every step has a verdict. Call `finish_run` to close the run.")

    if channel.operator_instructions:
        spoken = "\n".join(f"  - {message}" for message in channel.operator_instructions)
        # Deliberately the same wording `with_operator_messages` uses, so an
        # instruction reads on re-entry exactly as it read when it arrived.
        lines.append("")
        lines.append(f"The operator said, and it applies from now on:\n{spoken}")

    return "\n".join(lines)


def _summary_text_of(messages: list[BaseMessage]) -> str:
    """The summary out of what the middleware built, or "" if there is none."""
    for message in messages:
        source = (getattr(message, "additional_kwargs", None) or {}).get("lc_source")
        if source == _SUMMARY_SOURCE and isinstance(message.content, str):
            content = message.content
            _, _, body = content.partition(_SUMMARY_PREAMBLE)
            return (body or content).strip()
    return ""


def _insert_after_summary(
    messages: list[BaseMessage], ledger: BaseMessage
) -> list[BaseMessage]:
    """Put `ledger` between the summary and the preserved tail.

    The boundary is found by the marker LangChain tags summary messages with. That
    marker is not part of a documented contract, so a version that stops setting it
    must not silently drop the ledger: the fallback puts it right after the leading
    `RemoveMessage`, which is where the summary begins in the shape this middleware
    has always returned.
    """
    for index in range(len(messages) - 1, -1, -1):
        source = (getattr(messages[index], "additional_kwargs", None) or {}).get("lc_source")
        if source == _SUMMARY_SOURCE:
            return [*messages[: index + 1], ledger, *messages[index + 1 :]]
    return [*messages[:1], ledger, *messages[1:]]


def _folded_token_counter(messages) -> int:
    """Count what the model is actually sent, not what the graph is holding.

    `fold_stale_scenes` runs as a `wrap_model_call`, so it rewrites one request
    and leaves the graph's own messages alone. Counting those unfolded messages
    would measure a conversation that is never sent, and compaction would fire
    while there was still plenty of room — repeatedly, since folding keeps the
    real size flat while the stored size climbs.
    """
    return count_tokens_approximately(fold_stale_scenes(list(messages)))


class QaCompactionMiddleware(SummarizationMiddleware):
    """Summarize the old conversation, then restate what the run knows.

    Extends LangChain's summarization rather than reimplementing it, because the
    part that has to be exactly right — never cutting between a tool call and its
    result — is already there and already tested.
    """

    def __init__(
        self,
        *,
        model: BaseChatModel,
        run_model_max_input_tokens: int,
        state: QaRunState,
        channel: QaRunChannel,
        trigger_fraction: float,
        keep_messages: int,
        min_new_messages: int,
        trim_tokens: int,
        on_compacted: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> None:
        # The threshold is worked out here rather than handed over as
        # `("fraction", ...)`, because the base class would resolve a fraction
        # against `model` — the summarizer. That is the wrong window: the limit
        # this exists to stay under belongs to the model running the QA loop, and
        # the two are deliberately different models. A cheap summarizer would
        # otherwise drag the trigger down to its own smaller window and compact a
        # large-context run many times over for no reason.
        super().__init__(
            model,
            # Floored at 1: the base class rejects a threshold of 0, and a
            # misconfigured fraction should compact constantly rather than refuse
            # to build the run at all.
            trigger=("tokens", max(1, int(run_model_max_input_tokens * trigger_fraction))),
            keep=("messages", keep_messages),
            summary_prompt=QA_SUMMARY_PROMPT,
            trim_tokens_to_summarize=trim_tokens,
            token_counter=_folded_token_counter,
        )
        self._state = state
        self._channel = channel
        self._min_new_messages = min_new_messages
        self._on_compacted = on_compacted
        self._length_at_last_compaction = 0
        self._forced = False
        # `AgentMiddleware` declares `tools` without giving it a class-level
        # default, and the agent factory reads it with `getattr(..., [])`. An
        # instance that does not set it simply contributes no tools, silently.
        #
        # Registered here rather than in `build_tools` so that a run without this
        # middleware also has no `compact_context` — a tool that sets a flag
        # nothing reads is worse than an absent one.
        self.tools = [build_compact_tool(state)]

    def _should_summarize(self, messages: list[AnyMessage], total_tokens: int) -> bool:
        """Whether to compact now.

        Overrides the base class's decision to add the two things a QA run needs:
        the agent's own request, and a floor on how often either trigger can fire.

        The floor is about cost. A compaction pays for a summarizing call and then
        invalidates the prompt cache — it rewrites the prefix the cache is keyed
        on, so the next call reads nothing back and writes the whole thing again.
        Without a floor, a preserved tail that is itself over the threshold would
        do that on every single turn.
        """
        if len(messages) - self._length_at_last_compaction < self._min_new_messages:
            return False
        if self._forced:
            return True
        return super()._should_summarize(messages, total_tokens)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        # Consumed before anything is awaited: the request is answered by this
        # pass whether or not it results in a compaction, and a second pass must
        # not act on it again.
        self._forced = self._state.compaction_requested
        self._state.compaction_requested = False

        try:
            # Folded, so that everything downstream — the count, the cut point,
            # the text handed to the summarizer, and the tail written back to the
            # graph — works on the conversation as the model actually sees it.
            # Folding is idempotent and only ever replaces a stale scene view with
            # a placeholder, and the timeline and console read the channel and the
            # logger rather than graph state, so nothing readable is lost by
            # letting the folded copies persist.
            update = await super().abefore_model(
                {**state, "messages": fold_stale_scenes(state["messages"])}, runtime
            )
        finally:
            self._forced = False

        if update is None:
            return None

        messages = update["messages"]
        summary = _summary_text_of(messages)
        if not summary or summary.startswith(_SUMMARY_FAILURE_PREFIXES):
            # The base class turns a failed summarizing call into a summary that
            # says it failed, and would still return the instruction to delete
            # everything else. Declining the whole update is the only thing that
            # keeps a summarizer outage from costing the run its history; the run
            # continues uncompacted and may reach the provider's limit, which is
            # the lesser failure and one the operator can see coming.
            await self._channel.note(
                "Context compaction was skipped: the summary could not be "
                "generated. The run continues with its history intact.",
                LogCategory.SYSTEM,
            )
            return None

        update["messages"] = _insert_after_summary(
            messages, HumanMessage(content=render_progress_ledger(self._state, self._channel))
        )
        self._length_at_last_compaction = len(update["messages"])
        self._state.compactions += 1

        await self._channel.note(
            f"Context compacted (#{self._state.compactions}). Earlier turns were "
            "summarized; recorded verdicts and operator instructions were kept.",
            LogCategory.SYSTEM,
        )
        if self._on_compacted is not None:
            await self._on_compacted(summary, self._state.compactions)
        return update
