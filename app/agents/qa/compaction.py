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
  than summarized. Step verdicts, the steps still without one, everything the
  operator has said, and the screen as it stands are facts this process already
  holds; asking a model to remember them accurately would be trading certainty
  for nothing.

  화면이 여기 온 것은 ARTEL-622 부터다. 그 전에는 매 모델 호출 뒤에 붙는 꼬리가
  그 보장을 대신했는데, 그 꼬리가 프롬프트 접두를 매 턴 깨뜨려 캐시를 못 쓰게
  만들고 있었다(ARTEL-621). 보장을 압축 자신이 지도록 옮겼다 — 렌더 구조가 또
  바뀌어도 여기는 안 깨진다.

Two triggers, one mechanism: the automatic one at a fraction of the model's input
budget, and `compact_context`, which the agent calls when it judges its own
history to have become unwieldy.

The summarizing prompt is a prompt like any other and lives with the rest of
them, under `app/prompts/qa_compaction/`. Its own agent rather than a role under
`qa_run`, because it is a different call to a different model: tying it to the
run prompt's version would mean `QA_PROMPT_VERSION=v4`, the documented way to
roll a system-prompt change back, silently rolling this back too — and failing
outright on any version predating it.
"""

import logging
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

logger = logging.getLogger(__name__)

# 요약을 만들지 못했을 때 `SummarizationMiddleware`가 요약이라며 돌려주는 문자열.
# 예외를 올리는 대신 이렇게 보고하던 버전들의 이야기다. 요약 모델에서 나온 예외를
# 전부 잡아 이 문자열로 바꾼 뒤, 진짜 요약과 똑같은 경로로 돌려준다 — 그래서 대화가
# 통째로 지워지고 그 자리에 오류 문구가 남는다. 이 프리픽스가 그 경우를 요약과
# 구별하는 유일한 단서다.
#
# langchain 1.3.15부터는 같은 실패가 예외로 온다. 요약 실패가 이력을 영구 삭제하던
# 문제(langchain #38867)를 고치면서, 요약을 지어내지 않고 `with_retry()`가 재시도를
# 소진한 뒤 예외를 그대로 올리도록 바뀌었다. `abefore_model`은 양쪽을 모두 받는다.
# 두 버전이 다른 것은 실패가 도착하는 방식뿐이고, 런에게 무슨 뜻인지는 같기 때문이다.
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

    # 화면도 여기 온다. 판정과 같은 이유다 — 이미 데이터로 들고 있고, 요약 모델에게 맡기면
    # 가끔 틀리게 옮긴다.
    #
    # 종전에는 이 자리가 비어 있었고, 그 근거가 매 모델 호출 뒤에 붙던 꼬리였다("The current
    # screen needs no restating"). ARTEL-621 이 그 꼬리를 없앴다 — 프롬프트 접두를 매 턴
    # 깨뜨려 캐시가 시스템 프롬프트에서 멈추게 하고 있었기 때문이다. 그래서 이 보장을 압축
    # 자신이 진다. 렌더 구조가 또 바뀌어도 여기는 안 깨진다.
    #
    # `keep_messages` 로는 대신할 수 없다. 그것은 메시지 **개수**라 씬 경계와 무관하게 자르고,
    # 씬 페이지가 그 밖에 있으면 화면이 통째로 사라진다.
    # 아무것도 안 왔으면 싣지 않는다. `render` 는 그때 안내 문구를 내는데, 그것을 화면인
    # 척 원장에 얹으면 에이전트가 빈 화면을 실제 화면으로 읽는다 — 붙자마자 판독이 온다는
    # 것과 "안 와도 있는 척한다"는 다르다.
    if channel.scene.pulse.seen or channel.scene.frames > 0:
        lines.append("")
        lines.append("The screen as it stands right now:")
        lines.append(channel.scene.render(0))

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
        summary_prompt: str,
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
            # Passed in rather than read here, so prompt resolution stays in the
            # one place that already does it — see `QaRunner.run`, and
            # `app/prompts/qa_compaction/` for the text itself.
            summary_prompt=summary_prompt,
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

    async def _decline_compaction(self) -> None:
        """런이 계속 커지고 있음을 오퍼레이터에게 알리고, 그대로 둔다.

        업데이트 전체를 거절하는 것만이 요약 모델 장애가 런의 이력을 앗아가는 것을
        막는다. 런은 압축되지 않은 채 계속되고 provider의 한계에 닿을 수 있지만,
        그쪽이 덜 나쁜 실패이고 오퍼레이터가 다가오는 것을 볼 수 있는 실패다.
        """
        await self._channel.note(
            "Context compaction was skipped: the summary could not be "
            "generated. The run continues with its history intact.",
            LogCategory.SYSTEM,
        )

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        # Consumed before anything is awaited: the request is answered by this
        # pass whether or not it results in a compaction, and a second pass must
        # not act on it again.
        self._forced = self._state.compaction_requested
        self._state.compaction_requested = False

        # 폴딩해서 넘긴다. 그래야 이후의 모든 것 — 토큰 수, 자르는 지점, 요약 모델에
        # 건네는 텍스트, 그래프에 되돌려 쓰는 꼬리 — 이 모델이 실제로 보는 대화를
        # 기준으로 움직인다. 폴딩은 멱등하고 오래된 장면 뷰를 자리표시자로 바꾸는 일만
        # 하며, 타임라인과 콘솔은 그래프 상태가 아니라 채널과 로거를 읽는다. 그래서
        # 폴딩된 사본이 그대로 남아도 읽을 수 있는 것은 아무것도 잃지 않는다.
        #
        # `try` 앞에서 하는 것은 의도다. 이 폴딩의 버그가 요약 모델 장애로 둔갑해
        # 오퍼레이터에게 보고되지 않고 그 자체로 터지게 하려는 것이다. 다만 이 한 번의
        # 폴딩만 밖에 있다. `_folded_token_counter`가 base class 안에서 다시 폴딩하고,
        # 그 호출은 아래 핸들러 아래에 있다.
        folded = fold_stale_scenes(state["messages"])

        try:
            update = await super().abefore_model({**state, "messages": folded}, runtime)
        except Exception:
            # 넓게 잡는 것은 의도다. 여기까지 오는 것은 `with_retry()`가 포기한 뒤 요약
            # provider가 올린 무엇이든이고, 그 예외 타입은 provider마다 다르다. 좁히면
            # 구멍이 남고 그 대가는 런의 이력 전체다. 삼키지 않고 남기는 이유는,
            # 오퍼레이터에게 가는 노트가 요약이 실패했다고만 말하고 이유는 말하지 않기
            # 때문이다.
            logger.exception("[QA] context compaction failed; run continues uncompacted")
            await self._decline_compaction()
            return None
        finally:
            self._forced = False

        if update is None:
            return None

        messages = update["messages"]
        summary = _summary_text_of(messages)
        if not summary or summary.startswith(_SUMMARY_FAILURE_PREFIXES):
            # 실패한 요약 호출을 "실패했다는 요약"으로 보고하는 버전들은, 그러면서도
            # 나머지를 전부 지우라는 지시를 함께 돌려준다.
            await self._decline_compaction()
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
