"""`QaCompactionMiddleware` in isolation.

The end-to-end wiring is pinned in `tests/test_qa_run_compacted.py`. What is
tested here is the message surgery and the decisions around it, because those are
where a mistake is silent: a conversation that comes back missing one half of a
tool call is not rejected here, it is rejected by the provider, several turns
later, as a 400 with no mention of compaction in it.
"""

import asyncio
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.compaction import QaCompactionMiddleware, render_progress_ledger
from app.agents.qa.tools import QaRunState
from app.prompts import load_prompt
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, MessageType
from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX, SCENE_VIEW_START_SUFFIX
from app.qa.schemas import QaStepResult

SUMMARY_TEXT = "## SCENARIO\nA scenario.\n\n## NEXT ACTION\nCarry on."


class FakeSummarizer(BaseChatModel):
    """Stands in for the summarizing model, and counts how often it was asked."""

    summary: str = SUMMARY_TEXT
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "fake-summarizer"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.summary))])


def make_channel() -> tuple[QaRunChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return QaRunChannel(qa_try_id=1, send=send), sent


def scene_view(observation: int, body: str = "you can act on:\n  1 Start") -> str:
    """A tool result carrying a scene view, marked the way `SceneMemory` marks it."""
    start = f"{SCENE_VIEW_START_PREFIX}{observation}{SCENE_VIEW_START_SUFFIX}"
    return f"{start}\n{body}\n{SCENE_VIEW_END}"


def conversation(pairs: int, view_body: str = "you can act on:\n  1 Start") -> list[BaseMessage]:
    """`pairs` observe_scene round trips, each an AIMessage and its ToolMessage."""
    messages: list[BaseMessage] = [HumanMessage(content="Begin. Observe the screen first.")]
    for index in range(1, pairs + 1):
        call_id = f"call-{index}"
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "observe_scene", "args": {}, "id": call_id}],
            )
        )
        messages.append(
            ToolMessage(
                content=scene_view(index, view_body),
                tool_call_id=call_id,
                name="observe_scene",
            )
        )
    return messages


def build_middleware(
    state: QaRunState,
    channel: QaRunChannel,
    model: FakeSummarizer,
    *,
    # Small enough that any conversation these tests build is over the
    # threshold; the tests that need the opposite pass a large budget.
    max_input_tokens: int = 10,
    trigger_fraction: float = 0.9,
    keep_messages: int = 6,
    min_new_messages: int = 4,
) -> QaCompactionMiddleware:
    return QaCompactionMiddleware(
        model=model,
        # The real prompt, not a stub: `_acreate_summary` formats it with
        # `{messages}`, so a stub without that field would pass here and fail in
        # production.
        summary_prompt=load_prompt("qa_compaction", "summary").body,
        run_model_max_input_tokens=max_input_tokens,
        state=state,
        channel=channel,
        trigger_fraction=trigger_fraction,
        keep_messages=keep_messages,
        min_new_messages=min_new_messages,
        trim_tokens=4_000,
    )


def compact(middleware: QaCompactionMiddleware, messages: list[BaseMessage]):
    return asyncio.run(middleware.abefore_model({"messages": messages}, None))


def unanswered_calls(messages: list[BaseMessage]) -> list[str]:
    """Tool call ids with no result, and result ids with no call — either is a 400."""
    called = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in (message.tool_calls or [])
        if call.get("id")
    }
    answered = {
        message.tool_call_id
        for message in messages
        if isinstance(message, ToolMessage) and message.tool_call_id
    }
    return sorted(called.symmetric_difference(answered))


def test_a_cut_never_separates_a_tool_call_from_its_result() -> None:
    """The invariant the provider enforces, and the reason this subclasses
    LangChain's summarization rather than slicing the list itself.

    `keep=7` over this conversation puts the naive cut squarely on a ToolMessage:
    the tail would begin with a result whose call had just been summarized away.
    """
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    model = FakeSummarizer()
    middleware = build_middleware(state, channel, model, keep_messages=7)

    messages = conversation(pairs=10)
    assert isinstance(messages[len(messages) - 7], ToolMessage)

    update = compact(middleware, messages)

    assert update is not None
    assert unanswered_calls(update["messages"]) == []


def test_a_summarizer_that_fails_does_not_cost_the_run_its_history() -> None:
    """langchain 1.3.15부터는 재시도를 소진한 요약 호출이 미들웨어 밖으로 예외를
    올린다. 그것을 잡는 것이, 요약 모델 타임아웃과 판정을 절반만 기록한 채
    시나리오 중간에 죽는 런 사이에 서 있는 유일한 것이다."""
    state = QaRunState(total_steps=1)
    channel, sent = make_channel()
    middleware = build_middleware(state, channel, FakeSummarizer())

    async def summarizer_is_down(_messages):
        raise RuntimeError("the summarizer is down")

    # 모델이 아니라 심(seam)에서 예외를 던진다. 실패한 모델이 무엇을 만들어내는지가
    # 바로 버전마다 다른 부분이기 때문이다 — 1.3.15는 예외를 올리고, 그 이전 버전은
    # 아래 테스트가 다루는 실패 문자열로 바꾼다. 여기서 모델을 통해 실패시키면 설치된
    # 버전이 두 핸들러 중 어느 쪽을 태울지 정해버리고, 결국 어느 쪽도 양쪽 버전에서
    # 검증되지 않는다.
    middleware._acreate_summary = summarizer_is_down

    assert compact(middleware, conversation(pairs=10)) is None
    assert state.compactions == 0

    notes = [frame for frame in sent if frame["type"] == MessageType.LOG.value]
    assert notes, "the operator has to be told the run is still growing"
    assert notes[-1]["payload"]["category"] == LogCategory.SYSTEM.value


def test_a_summary_that_reports_its_own_failure_is_declined_too() -> None:
    """langchain 1.3.15 이전에는 같은 장애가 "실패했다는 요약"으로 도착했고, 나머지를
    전부 지우라는 지시가 여전히 붙어 있었다. 설치된 버전이 두 경로 중 어느 쪽이
    도는지를 정하므로, 위의 실패 테스트가 닿지 못하는 쪽을 여기서 고정한다."""
    state = QaRunState(total_steps=1)
    channel, sent = make_channel()
    model = FakeSummarizer(summary="Error generating summary: the summarizer is down")
    middleware = build_middleware(state, channel, model)

    messages = conversation(pairs=10)
    assert compact(middleware, messages) is None
    assert state.compactions == 0

    notes = [frame for frame in sent if frame["type"] == MessageType.LOG.value]
    assert notes, "the operator has to be told the run is still growing"
    assert notes[-1]["payload"]["category"] == LogCategory.SYSTEM.value


def test_the_agent_can_compact_below_the_automatic_threshold() -> None:
    """`compact_context` exists for the run the size trigger has not caught yet."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    model = FakeSummarizer()
    # A budget nothing here will ever approach, so only the flag can fire it.
    middleware = build_middleware(state, channel, model, max_input_tokens=10_000_000)
    messages = conversation(pairs=10)

    assert compact(middleware, messages) is None

    state.compaction_requested = True
    update = compact(middleware, messages)

    assert update is not None
    assert state.compactions == 1
    # Consumed by the pass that answered it: a request is for one compaction.
    assert state.compaction_requested is False
    assert compact(middleware, messages) is None


def test_the_size_trigger_fires_on_the_run_model_s_budget() -> None:
    """Above the fraction it compacts; below it, it leaves the run alone."""
    messages = conversation(pairs=10)
    size = count_tokens_approximately(messages)

    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    over = build_middleware(
        state, channel, FakeSummarizer(), max_input_tokens=int(size / 0.9) - 10
    )
    assert compact(over, messages) is not None

    idle_state = QaRunState(total_steps=1)
    idle_channel, _ = make_channel()
    under = build_middleware(
        idle_state, idle_channel, FakeSummarizer(), max_input_tokens=int(size / 0.9) * 4
    )
    assert compact(under, messages) is None


def test_the_threshold_is_measured_on_the_folded_conversation() -> None:
    """`fold_stale_scenes` rewrites the request, not the graph's own messages, so
    counting what is stored would measure a conversation that is never sent — and
    keep measuring it as bigger every turn while the real size stays flat."""
    # Long enough that folding all but the newest view is the difference between
    # over and under the threshold.
    messages = conversation(pairs=10, view_body="you can act on:\n" + "  1 Start\n" * 200)
    stored = count_tokens_approximately(messages)

    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    model = FakeSummarizer()
    # A budget the stored size clears but the folded size does not.
    middleware = build_middleware(
        state, channel, model, max_input_tokens=int(stored / 0.9)
    )

    assert compact(middleware, messages) is None
    assert model.calls == 0


def test_nothing_compacts_again_until_the_conversation_has_moved_on() -> None:
    """The thrash guard. Each compaction pays for a summary and then invalidates
    the prompt cache it rewrote the prefix of; a preserved tail that is itself over
    the threshold would otherwise do that on every single turn."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    model = FakeSummarizer()
    middleware = build_middleware(state, channel, model, max_input_tokens=10)

    first = compact(middleware, conversation(pairs=10))
    assert first is not None
    calls_after_first = model.calls

    # Two more messages is short of `min_new_messages`, so nothing happens even
    # though the size trigger is still screaming.
    grown = [*first["messages"][1:], HumanMessage(content="hi"), AIMessage(content="ok")]
    assert compact(middleware, grown) is None
    assert model.calls == calls_after_first


def test_the_ledger_restates_what_the_summary_is_told_not_to_carry() -> None:
    """Verdicts, the steps still open, and the operator's standing instructions are
    facts the run already holds. Asking a model to remember them would trade
    certainty for nothing."""
    state = QaRunState(total_steps=4)
    state.step_results = [
        QaStepResult(step=1, passed=True, message="시작 화면이 떴다"),
        QaStepResult(step=2, passed=False, message="설정 버튼이 없다"),
    ]
    channel, _sent = make_channel()
    channel.on_chat({"payload": {"message": "느리게 진행해줘"}})
    channel.on_chat({"payload": {"message": "소리는 끄고"}})

    ledger = render_progress_ledger(state, channel)

    assert "step 1: PASS — 시작 화면이 떴다" in ledger
    assert "step 2: FAIL — 설정 버튼이 없다" in ledger
    assert "3, 4" in ledger
    assert "Continue with step 3" in ledger
    assert "느리게 진행해줘" in ledger
    assert "소리는 끄고" in ledger
    # The same wording the tools use, so an instruction reads on re-entry exactly
    # as it read when it arrived.
    assert "The operator said, and it applies from now on:" in ledger


def test_the_ledger_reaches_the_model_after_the_summary() -> None:
    state = QaRunState(total_steps=2)
    state.step_results = [QaStepResult(step=1, passed=True, message="확인함")]
    channel, _sent = make_channel()
    middleware = build_middleware(state, channel, FakeSummarizer(), max_input_tokens=10)

    update = compact(middleware, conversation(pairs=10))

    assert update is not None
    texts = [str(message.content) for message in update["messages"]]
    summary_at = next(index for index, text in enumerate(texts) if SUMMARY_TEXT in text)
    ledger_at = next(index for index, text in enumerate(texts) if "CONTEXT COMPACTED" in text)
    assert ledger_at == summary_at + 1
    assert "step 1: PASS — 확인함" in texts[ledger_at]


def test_compaction_cannot_lose_a_verdict() -> None:
    """`step_results` lives on `QaRunState`, never in the message list — which is
    what makes the ledger able to restate it. Asserted rather than assumed."""
    state = QaRunState(total_steps=2)
    state.step_results = [QaStepResult(step=1, passed=True, message="확인함")]
    channel, _sent = make_channel()
    middleware = build_middleware(state, channel, FakeSummarizer(), max_input_tokens=10)

    compact(middleware, conversation(pairs=10))

    assert [result.step for result in state.step_results] == [1]


def test_langchain_still_offers_the_seams_this_is_built_on() -> None:
    """This test exists to fail on a LangChain upgrade, not to test our code.

    Compaction hangs off three things the library does not document as API: the
    decision point `_should_summarize`, the summarizing call `_acreate_summary`,
    and the shape `abefore_model` returns. If any of them moves, compaction stops
    happening — silently, which is the dangerous way for it to break, since the
    symptom is a provider 400 much later in a run.
    """
    import inspect

    from langchain.agents.middleware import SummarizationMiddleware
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    signature = inspect.signature(SummarizationMiddleware._should_summarize)
    assert list(signature.parameters) == ["self", "messages", "total_tokens"]

    # `test_a_summarizer_that_fails_does_not_cost_the_run_its_history`가 예외를
    # 던지는 심. 지원하는 모든 버전에서 `abefore_model`이 이것을 await하고, 그래서
    # 그 테스트가 어느 버전에서나 같은 것을 검증한다.
    assert inspect.iscoroutinefunction(SummarizationMiddleware._acreate_summary)

    base = SummarizationMiddleware(
        FakeSummarizer(), trigger=("tokens", 1), keep=("messages", 4)
    )
    update = asyncio.run(base.abefore_model({"messages": conversation(pairs=10)}, None))
    assert update is not None
    first = update["messages"][0]
    assert isinstance(first, RemoveMessage)
    assert first.id == REMOVE_ALL_MESSAGES


def test_the_tool_only_raises_a_flag() -> None:
    """It cannot do the work itself: its own AIMessage is already in the
    conversation, and rewriting the list from the tools node would delete that
    message while leaving this result behind it."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    middleware = build_middleware(state, channel, FakeSummarizer())

    (compact_context,) = middleware.tools
    assert compact_context.name == "compact_context"

    answer = asyncio.run(compact_context.ainvoke({"reason": "너무 길어졌다"}))

    assert state.compaction_requested is True
    assert "preserved" in answer


def test_the_summary_prompt_is_versioned_like_every_other_prompt() -> None:
    """It is a prompt, so it lives with the prompts rather than in a constant —
    reviewable, diffable, and pinnable to a candidate without a deploy.

    Its own agent, not a role under `qa_run`: `QA_PROMPT_VERSION=v4` is the
    documented way to roll a system-prompt change back, and it must not take this
    with it — nor fail outright on a version that predates it.
    """
    from app.prompts import SETTINGS_VERSION_KEYS, available_versions

    assert SETTINGS_VERSION_KEYS["qa_compaction"] == "qa_compaction_prompt_version"
    assert available_versions("qa_compaction")[0] == "v1"

    prompt = load_prompt("qa_compaction", "summary")
    # `_acreate_summary` calls `.format(messages=...)`, and LangChain documents the
    # `<messages>` marker as part of the constant's contract — deep agents splice
    # instructions in just above it. The loader pins the placeholder; this pins the
    # marker, which it cannot see.
    assert prompt.placeholders == ("messages",)
    assert "<messages>" in prompt.body

    # And it still says the things the ledger depends on it NOT duplicating.
    assert "restated" in prompt.body
    assert "Never write that a step passed or failed" in prompt.body


def _screen_arrives(channel: QaRunChannel) -> None:
    """게임이 화면을 하나 올린다. 판독이 유일한 출처인 지금의 모양으로."""
    from app.qa.pulse import PulseReading

    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "schema": 2,
                "reading": 1,
                "frame": 100,
                "scene": "Map_scene",
                "whole": True,
                "statics": [
                    {"declaring": "Core.InteractionLock", "member": "IsLocked", "value": True}
                ],
                "active": [
                    {
                        "selector": "TutorialController",
                        "id": 9001,
                        "members": [
                            {
                                "member": "waitingForAcknowledge",
                                "value": True,
                                "on": "Tutorial.TutorialController",
                            }
                        ],
                    }
                ],
                "deactive": [],
                "changed": [],
            }
        )
    )


def test_원장이_지금_화면을_들고_간다() -> None:
    """판정과 같은 이유다 — 이미 데이터로 들고 있고, 요약 모델에게 맡기면 가끔 틀리게 옮긴다.

    종전에는 이 보장이 매 모델 호출 뒤에 붙는 꼬리에 걸려 있었다. 그 꼬리가 프롬프트
    접두를 매 턴 깨뜨려 캐시를 못 쓰게 만들고 있었으므로 없앴고(ARTEL-621), 보장을
    압축 자신이 지도록 옮겼다(ARTEL-622)."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    _screen_arrives(channel)

    ledger = render_progress_ledger(state, channel)

    assert "The screen as it stands right now:" in ledger
    assert "InteractionLock.IsLocked = True" in ledger
    assert "TutorialController.waitingForAcknowledge = True" in ledger


def test_화면이_없으면_있는_척하지_않는다() -> None:
    """붙자마자 판독이 온다는 것과 "안 와도 있는 척한다"는 다르다. 뒤의 것이면
    에이전트가 빈 화면을 실제 화면으로 읽는다."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()

    ledger = render_progress_ledger(state, channel)

    assert "The screen as it stands right now:" not in ledger


def test_압축이_걸려도_화면이_남는다() -> None:
    """자동 발동 경로. 압축 뒤 모델이 받는 것에 화면이 있어야 한다."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    _screen_arrives(channel)
    model = FakeSummarizer()
    # 이 대화가 반드시 넘길 만큼 낮은 예산으로 강제로 태운다.
    middleware = build_middleware(state, channel, model, max_input_tokens=100)

    update = compact(middleware, conversation(pairs=10))

    assert update is not None
    body = "\n".join(str(m.content) for m in update["messages"])
    assert "InteractionLock.IsLocked = True" in body


def test_에이전트가_스스로_압축해도_화면이_남는다() -> None:
    """`compact_context` 경로. 자동 발동만 막으면 이쪽으로 새어 나간다."""
    state = QaRunState(total_steps=1)
    channel, _sent = make_channel()
    _screen_arrives(channel)
    model = FakeSummarizer()
    middleware = build_middleware(state, channel, model, max_input_tokens=10_000_000)

    state.compaction_requested = True
    update = compact(middleware, conversation(pairs=10))

    assert update is not None
    body = "\n".join(str(m.content) for m in update["messages"])
    assert "InteractionLock.IsLocked = True" in body


def test_원장이_압축_전에_지식을_남기라고_말한다() -> None:
    """압축이 바로 그 순간이다. 앞의 상세가 여기서 사라지므로, 그것으로만 알 수 있던 것을
    남길 마지막 기회다.

    긴 런일수록 못 적는다 — 알아낸 것이 제일 많은 런이 제일 많이 잃는다. 마지막 스텝
    판정에서 말하는 것(ARTEL-667)은 그때까지 문맥이 남아 있는 런에만 닿는다.
    """
    state = QaRunState(total_steps=2)
    state.step_results = [QaStepResult(step=1, passed=True, message="봤다")]
    channel, _sent = make_channel()

    ledger = render_progress_ledger(state, channel)

    assert "record_knowledge" in ledger, ledger
    assert "while you still remember it" in ledger


def test_이미_적은_런에게는_원장이_말하지_않는다() -> None:
    """이미 적고 있는 런에게 또 시키면 표가 뜻을 잃는다."""
    state = QaRunState(total_steps=2)
    state.knowledge_records_attempted = 1
    channel, _sent = make_channel()

    assert "record_knowledge" not in render_progress_ledger(state, channel)
