"""The knowledge tools: the round trips, and every way they do not work.

These three are the only tools that talk past the game — their correspondent is
Orchestration's knowledge base rather than the SDK — so nothing in
`tests/test_qa_tools.py` exercises this path. What is pinned here is what the run
keeps doing when an answer is empty, late, refused, or spent: in every one of
those the agent has to come back with something it can judge a step on, because
knowledge is a side errand to the verdict and never a reason to stop.

The writes carry a second burden the search does not. There is no update tool, so
correcting an entry is `forget_knowledge` followed by `record_knowledge`, and the
delete lands on the far side before the record is even attempted. "Deleted, then
failed to record" is the one path in this design that loses knowledge outright,
and it has its own section below.
"""

import asyncio

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.knowledge import (
    KNOWLEDGE_TAGS,
    MAX_DESCRIPTION_CHARS,
    MAX_FORGETS_PER_RUN,
    MAX_RECORDS_PER_RUN,
    MAX_SEARCHES_PER_RUN,
    RESULT_LIMIT,
    render_description,
)
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, MessageType
from app.qa.scene import SCENE_VIEW_START_PREFIX


def make(timeout: float = 0.05):
    """A channel whose knowledge search times out fast unless a test answers it."""
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout)
    state = QaRunState(total_steps=1)
    tools = {tool.name: tool for tool in build_tools(channel, state)}
    return channel, state, tools, sent


def make_with_undeliverable(*types: MessageType):
    """A channel that refuses the named frame types and carries everything else.

    An undelivered frame is the only failure of a knowledge write this side can
    observe at all: nothing answers a write, so a rejection on the far side is
    invisible here, and a frame that never left is the whole of what "it failed"
    can mean.

    Refusing one type rather than all of them is what makes the worst case
    reproducible — a delete that lands followed by a record that cannot be sent.
    The timeline logging is left working on purpose: a send that fails for every
    frame is a dead socket, and a dead socket is meant to end the run through
    whichever tool touches it next rather than be absorbed here.
    """
    refused = {message_type.value for message_type in types}
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        if frame["type"] in refused:
            raise ConnectionResetError("the frame could not be delivered")
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05)
    state = QaRunState(total_steps=1)
    tools = {tool.name: tool for tool in build_tools(channel, state)}
    return channel, state, tools, sent


def searches(sent: list[dict]) -> list[dict]:
    return [f for f in sent if f["type"] == MessageType.KNOWLEDGE_SEARCH.value]


def creates(sent: list[dict]) -> list[dict]:
    return [f for f in sent if f["type"] == MessageType.KNOWLEDGE_CREATE.value]


def deletes(sent: list[dict]) -> list[dict]:
    return [f for f in sent if f["type"] == MessageType.KNOWLEDGE_DELETE.value]


async def record(tools: dict, **overrides) -> str:
    args = {
        "step": 1,
        "thought": "런에서 알게 된 규칙을 남긴다",
        "tag": "RULE",
        "summary": "구매는 소지금이 가격 이상일 때만 가능하다",
        "description": "소지금이 가격보다 적으면 구매 버튼이 비활성 상태가 된다.",
        **overrides,
    }
    return await tools["record_knowledge"].ainvoke(args)


async def forget(tools: dict, **overrides) -> str:
    args = {
        "step": 1,
        "thought": "관측이 이 항목과 반복해서 어긋난다",
        "knowledge_id": "41",
        **overrides,
    }
    return await tools["forget_knowledge"].ainvoke(args)


def hit(**overrides) -> dict:
    return {
        "id": "41",
        "tag": "RULE",
        "source": "DOCS",
        "summary": "구매는 소지금이 가격 이상일 때만 가능하다",
        "description": "소지금이 가격보다 적으면 구매 버튼은 비활성 상태가 되고 눌러도 반응하지 않는다.",
        "score": 0.83,
        **overrides,
    }


def answer(
    channel: QaRunChannel,
    sent: list[dict],
    payload: dict,
    type_: str | None = None,
    correlation: str | None = None,
):
    """Reply the way Orchestration does, quoting the request's messageId.

    Waits for the frame to actually go out rather than assuming it is already
    there when the replying task first runs. `correlation` overrides that echo,
    which is how a reply to somebody else's request is simulated.
    """
    already = len(searches(sent))

    async def reply() -> None:
        for _ in range(50):
            if len(searches(sent)) > already:
                break
            await asyncio.sleep(0)
        frame = {
            "type": type_ or MessageType.KNOWLEDGE_SEARCH_RESULT.value,
            "correlationId": correlation or searches(sent)[-1]["messageId"],
            "payload": payload,
        }
        if frame["type"] == MessageType.ERROR.value:
            channel.on_error(frame)
        else:
            channel.on_knowledge_search_result(frame)

    return asyncio.create_task(reply())


# --- the round trip -----------------------------------------------------------


def test_a_search_goes_out_as_a_knowledge_search_frame() -> None:
    """The type and the payload shape are the contract with Orchestration."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "골드 부족", "model": "e5", "results": [hit()]})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 2, "thought": "구매 실패 규칙을 확인한다", "query": "골드가 모자라면 어떻게 되나"}
        )

        frame = searches(sent)[-1]
        assert frame["payload"] == {
            "query": "골드가 모자라면 어떻게 되나",
            "tag": None,
            "limit": RESULT_LIMIT,
        }
        assert "구매는 소지금이" in result

    asyncio.run(run())


def test_the_reason_for_searching_reaches_the_timeline() -> None:
    """`thought` is the only record of why the run spent a search."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": []})
        await tools["search_knowledge"].ainvoke(
            {"step": 3, "thought": "규칙이 애매해 문서를 본다", "query": "구매 규칙"}
        )

        rows = [f for f in sent if f["type"] == MessageType.LOG.value]
        assert [(r["payload"]["message"], r["payload"]["step"]) for r in rows] == [
            ("규칙이 애매해 문서를 본다", 3)
        ]
        assert rows[0]["payload"]["category"] == LogCategory.THOUGHT.value

    asyncio.run(run())


def test_a_tag_narrows_the_search_and_travels_as_the_singular_field() -> None:
    """Orchestration takes `tag` or `tags`; this side settled on the singular."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": [hit()]})
        await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "규칙만 본다", "query": "구매 규칙", "tag": "rule"}
        )

        assert searches(sent)[-1]["payload"]["tag"] == "RULE"

    asyncio.run(run())


def test_a_hit_carries_where_it_came_from_and_how_close_it_is() -> None:
    """A weak match is still returned, so the agent must be able to discount it."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(
            channel,
            sent,
            {"query": "q", "model": "e5", "results": [hit(score=0.41, tag="OBJECTIVE")]},
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "승리 조건"}
        )

        assert "OBJECTIVE" in result
        assert "DOCS" in result
        assert "0.41" in result

    asyncio.run(run())


def test_a_long_description_is_clipped_and_says_so() -> None:
    """Results are never folded, so one verbose entry would sit in context for
    the rest of the run. Clipped rather than dropped: the agent still gets the
    beginning, and the marker tells it there was more."""
    clipped = render_description("가" * (MAX_DESCRIPTION_CHARS + 200))

    assert "[truncated]" in clipped
    assert len(clipped) < MAX_DESCRIPTION_CHARS + 200
    assert render_description("짧다") == "짧다"


# --- empty, late, refused, spent ---------------------------------------------


def test_an_empty_result_is_answered_as_an_answer() -> None:
    """The backfill is asynchronous, so "nothing indexed yet" is a normal state.

    Reported as an error it would push the agent to retry or to fail the step
    over knowledge that was never going to arrive during this run.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": []})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "not an error" in result
        assert "Judge this step on what you can see" in result

    asyncio.run(run())


def test_a_search_nobody_answers_gives_up_and_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run must not sit on a knowledge lookup; nothing answers here at all.

    The timeout is shortened rather than waited out — what is under test is what
    the agent is told afterwards, not how long the wait is.
    """
    monkeypatch.setattr("app.qa.channel.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 0.01)

    async def run() -> None:
        _, _, tools, _ = make()

        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "did not answer in time" in result

    asyncio.run(run())


def test_a_refused_search_keeps_the_run_going() -> None:
    """Orchestration answers a failed search with an ERROR on the same correlation.

    Its payload carries only `message` — no `code` — so the frame must be read
    leniently. Validated as a full ErrorPayload it would be dropped, and the tool
    that asked would hang to its timeout instead of being released.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(
            channel,
            sent,
            {"message": "KNOWLEDGE_SEARCH failed: embedding model mismatch"},
            type_=MessageType.ERROR.value,
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "embedding model mismatch" in result
        assert "says nothing about the game" in result

    asyncio.run(run())


def test_the_per_run_cap_is_refused_with_its_reason() -> None:
    """Same reason as the capture budget: a run that keeps looking things up
    instead of deciding reaches its deadline with no verdict to report."""

    async def run() -> None:
        channel, state, tools, sent = make()
        state.knowledge_searches_attempted = MAX_SEARCHES_PER_RUN

        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert str(MAX_SEARCHES_PER_RUN) in result
        assert searches(sent) == []

    asyncio.run(run())


def test_a_failed_search_still_spends_the_run_budget() -> None:
    """Counting only what worked would leave a failing search unbounded."""

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, {"message": "no"}, type_=MessageType.ERROR.value)
        await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert state.knowledge_searches_attempted == 1

    asyncio.run(run())


def test_an_unknown_tag_costs_neither_a_round_trip_nor_a_search() -> None:
    """Orchestration rejects the whole search on a tag it does not know, so the
    check belongs on this side of the socket."""

    async def run() -> None:
        _, state, tools, sent = make()

        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙", "tag": "GAMEPLAY"}
        )

        assert searches(sent) == []
        assert state.knowledge_searches_attempted == 0
        for name in KNOWLEDGE_TAGS:
            assert name in result

    asyncio.run(run())


def test_a_reply_to_a_foreign_request_does_not_resolve_this_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that arrives after its own search gave up must not be handed to
    whatever asked next — the agent would read one question's answer as another's."""
    monkeypatch.setattr("app.qa.channel.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 0.05)

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(
            channel,
            sent,
            {"query": "q", "model": "e5", "results": [hit()]},
            correlation="a-request-we-already-gave-up-on",
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "did not answer in time" in result
        assert "구매는 소지금이" not in result

    asyncio.run(run())


# --- ARTEL-180: a lookup must not drag a scene view back in -------------------


def test_a_search_neither_touches_the_game_nor_returns_a_scene() -> None:
    """The regression this tool was most likely to cause.

    Every acting tool answers with the scene it produced, and `fold_stale_scenes`
    exists because those views pile up. A search changes no screen, so returning
    one would re-spend the context that fold was written to save — and it would
    also cost a game round trip for a picture the agent already has.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_game_state(
            {
                "payload": {
                    "scene": "Shop",
                    "interactables": [{"id": 1, "name": "Buy", "type": "button"}],
                }
            }
        )
        frames_before = channel.scene.frames

        answer(channel, sent, {"query": "q", "model": "e5", "results": [hit()]})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert SCENE_VIEW_START_PREFIX not in result
        assert "you can act on:" not in result
        # No ACTION went out, so nothing asked the game for a scene either.
        assert [f for f in sent if f["type"] == MessageType.ACTION.value] == []
        assert channel.scene.frames == frames_before

    asyncio.run(run())


# --- what the model is handed -------------------------------------------------


def test_the_description_states_its_budget_and_its_tags() -> None:
    """An agent that learns the limit by hitting it has already spent it, and a
    tag list that drifts from Orchestration's turns every filter into a refusal."""
    _, _, tools, _ = make()
    description = tools["search_knowledge"].description

    assert str(MAX_SEARCHES_PER_RUN) in description
    for name in KNOWLEDGE_TAGS:
        assert name in description


def test_the_description_says_when_not_to_use_it() -> None:
    """The load-bearing half. Without it the agent searches every step, which
    costs the tool-call budget and returns documentation where it should have
    looked at the screen."""
    _, _, tools, _ = make()
    description = tools["search_knowledge"].description

    assert "Do NOT" in description
    assert "observe_scene" in description


def test_an_operator_message_still_reaches_the_agent_through_a_search() -> None:
    """Operator words ride on the next tool result whatever that tool is; a
    search that swallowed them would lose an instruction the run must obey."""

    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_chat({"payload": {"message": "상점은 건너뛰세요"}})

        answer(channel, sent, {"query": "q", "model": "e5", "results": []})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "상점은 건너뛰세요" in result

    asyncio.run(run())


# --- the writes: what goes on the wire ----------------------------------------


def test_a_record_goes_out_as_a_knowledge_create_frame() -> None:
    """The type and payload shape are the contract with Orchestration.

    `KnowledgeMutationRequest` reads `tag`, `summary` and `description` and takes
    the project, the source and the source id from the run itself — so a frame
    that named any of those would be ignored at best.
    """

    async def run() -> None:
        _, _, tools, sent = make()

        result = await record(tools)

        assert creates(sent)[-1]["payload"] == {
            "tag": "RULE",
            "summary": "구매는 소지금이 가격 이상일 때만 가능하다",
            "description": "소지금이 가격보다 적으면 구매 버튼이 비활성 상태가 된다.",
        }
        assert "RULE" in result

    asyncio.run(run())


def test_a_deletion_goes_out_as_a_knowledge_delete_frame_naming_only_the_id() -> None:
    """Orchestration maps this field with `@JsonProperty("knowledge_id")`, and ids
    travel as text so a 64-bit value cannot lose precision through JSON."""

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_seen["41"] = "구매는 소지금이 가격 이상일 때만 가능하다"

        await forget(tools)

        assert deletes(sent)[-1]["payload"] == {"knowledge_id": "41"}

    asyncio.run(run())


def test_a_write_does_not_wait_for_an_answer_that_never_comes() -> None:
    """`routeKnowledgeMutation` replies with no frame at all — a success is silent
    and a rejection becomes a row on the run's own timeline, not a frame back down
    this socket. A tool that waited would hang to its timeout on every call,
    including the ones that worked, so both writes must return with nothing
    inbound at all."""

    async def run() -> None:
        _, state, tools, _ = make()
        state.knowledge_seen["41"] = "옛 규칙"

        # Far below KNOWLEDGE_SEARCH_TIMEOUT_SECONDS: anything that waits fails here.
        await asyncio.wait_for(record(tools), timeout=1.0)
        await asyncio.wait_for(forget(tools), timeout=1.0)

    asyncio.run(run())


def test_the_reason_for_writing_reaches_the_timeline() -> None:
    """`thought` is the only record of why the knowledge base changed, and on a
    deletion it is the only thing a reviewer can weigh the deletion against."""

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_seen["41"] = "옛 규칙"

        await record(tools, step=2, thought="새 규칙을 남긴다")
        await forget(tools, step=3, thought="이 항목은 더 이상 사실이 아니다")

        rows = [f for f in sent if f["type"] == MessageType.LOG.value]
        assert [(r["payload"]["message"], r["payload"]["step"]) for r in rows] == [
            ("새 규칙을 남긴다", 2),
            ("이 항목은 더 이상 사실이 아니다", 3),
        ]
        assert {r["payload"]["category"] for r in rows} == {LogCategory.THOUGHT.value}

    asyncio.run(run())


# --- the writes: what is refused ----------------------------------------------


def test_an_entry_the_run_never_read_cannot_be_deleted() -> None:
    """The guard that decides whether the agent can erase things it has not seen.

    Orchestration resolves the id to a real row and has no way to know the agent
    never read it, so this check exists on this side or nowhere.
    """

    async def run() -> None:
        _, state, tools, sent = make()

        result = await forget(tools, knowledge_id="99")

        assert deletes(sent) == []
        assert state.knowledge_forgets_attempted == 0
        assert "search_knowledge" in result

    asyncio.run(run())


def test_a_search_is_what_makes_an_entry_deletable() -> None:
    """The other half of the same rule: an id that came back from a search in this
    run is accepted, and the id has to be printed for that to be usable at all."""

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": [hit(id="41")]})
        found = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "41" in found, "the agent cannot quote an id it was never shown"
        assert state.knowledge_seen["41"] == "구매는 소지금이 가격 이상일 때만 가능하다"

        await forget(tools, knowledge_id="41")

        assert deletes(sent)[-1]["payload"] == {"knowledge_id": "41"}

    asyncio.run(run())


def test_the_same_entry_cannot_be_deleted_twice() -> None:
    """A second delete would be refused on the far side as "not found" and the
    refusal would never come back, so the run would believe it deleted something."""

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_seen["41"] = "옛 규칙"

        await forget(tools)
        result = await forget(tools)

        assert len(deletes(sent)) == 1
        assert "you can only delete what you have read" in result

    asyncio.run(run())


def test_an_unknown_tag_records_nothing_and_says_what_to_use() -> None:
    """Orchestration rejects an unknown topic and its rejection never comes back
    down this socket, so a frame sent anyway would leave the run believing it had
    written something."""

    async def run() -> None:
        _, state, tools, sent = make()

        result = await record(tools, tag="GAMEPLAY")

        assert creates(sent) == []
        assert state.knowledge_records_attempted == 0
        for name in KNOWLEDGE_TAGS:
            assert name in result

    asyncio.run(run())


@pytest.mark.parametrize("blank", ["summary", "description"])
def test_a_blank_field_records_nothing(blank: str) -> None:
    """Same reason as the tag: rejected on arrival, and silently."""

    async def run() -> None:
        _, _, tools, sent = make()

        result = await record(tools, **{blank: "   "})

        assert creates(sent) == []
        assert "nothing was recorded" in result

    asyncio.run(run())


def test_each_write_has_its_own_per_run_cap() -> None:
    """Knowledge tidying must not eat the steps. The deletion budget is the
    smaller of the two because deleting is the less reversible act."""

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_records_attempted = MAX_RECORDS_PER_RUN
        state.knowledge_forgets_attempted = MAX_FORGETS_PER_RUN
        state.knowledge_seen["41"] = "옛 규칙"

        recorded = await record(tools)
        forgotten = await forget(tools)

        assert creates(sent) == [] and deletes(sent) == []
        assert str(MAX_RECORDS_PER_RUN) in recorded
        assert str(MAX_FORGETS_PER_RUN) in forgotten
        # The way out of a spent deletion budget is to report, not to delete.
        assert "report_step" in forgotten
        assert MAX_FORGETS_PER_RUN < MAX_RECORDS_PER_RUN

    asyncio.run(run())


def test_an_undeliverable_write_is_reported_rather_than_raised() -> None:
    """A failed knowledge write is a side errand that failed, not a failed run."""

    async def run() -> None:
        _, state, tools, _ = make_with_undeliverable(
            MessageType.KNOWLEDGE_CREATE, MessageType.KNOWLEDGE_DELETE
        )
        state.knowledge_seen["41"] = "옛 규칙"

        recorded = await record(tools)
        forgotten = await forget(tools)

        assert "could not be sent" in recorded
        assert "Nothing was recorded" in recorded
        # Nothing left the socket, so nothing was deleted and nothing is owed.
        assert "the entry is still on file" in forgotten
        assert state.knowledge_deleted_unreplaced == []

    asyncio.run(run())


# --- deleted, then not recorded: the one path that loses knowledge ------------


def test_a_deletion_tells_the_agent_the_repair_is_only_half_done() -> None:
    """There is no update tool, so `record_knowledge` is the other half and nothing
    calls it but the agent. The result is the last place that can say so."""

    async def run() -> None:
        _, state, tools, _ = make()
        state.knowledge_seen["41"] = "구매는 소지금이 가격 이상일 때만 가능하다"

        result = await forget(tools)

        assert "record_knowledge" in result
        assert "cannot be undone" in result
        # It names what it removed, not just the id.
        assert "구매는 소지금이" in result
        assert state.knowledge_deleted_unreplaced == [
            '41 — "구매는 소지금이 가격 이상일 때만 가능하다"'
        ]

    asyncio.run(run())


@pytest.mark.parametrize(
    "broken", [{"tag": "GAMEPLAY"}, {"summary": ""}, {"description": ""}]
)
def test_a_record_that_fails_after_a_deletion_says_what_is_now_missing(
    broken: dict,
) -> None:
    """THE scenario this design has to survive.

    The delete is already applied on the far side and cannot be undone from here.
    Every way the following record can be refused has to name the entry that is
    now missing and demand another attempt — a refusal phrased only as "nothing
    was recorded" reads as harmless, and the run moves on having erased a rule.
    """

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_seen["41"] = "구매는 소지금이 가격 이상일 때만 가능하다"

        await forget(tools)
        result = await record(tools, **broken)

        assert creates(sent) == []
        assert "NOTHING WAS RECORDED" in result
        assert "구매는 소지금이" in result
        assert "missing from the project right now" in result
        assert "call `record_knowledge` again immediately" in result
        # Still outstanding: only a write that actually goes out clears it.
        assert state.knowledge_deleted_unreplaced != []

    asyncio.run(run())


def test_the_worst_case_end_to_end_the_delete_lands_and_the_record_cannot_be_sent() -> None:
    """Knowledge actually goes missing here, and nothing can put it back.

    The other failures in this section are ones the agent caused and can fix by
    calling again with better arguments. This one it did not and cannot: the
    delete is gone down the socket, the record will not go, and the only thing
    left is that the run is told exactly what it no longer has.
    """

    async def run() -> None:
        _, state, tools, sent = make_with_undeliverable(MessageType.KNOWLEDGE_CREATE)
        state.knowledge_seen["41"] = "구매는 소지금이 가격 이상일 때만 가능하다"

        await forget(tools)
        assert deletes(sent) != [], "the deletion has to have really gone out"

        result = await record(tools)

        assert creates(sent) == []
        assert "could not be sent" in result
        assert "NOTHING WAS RECORDED" in result
        assert "구매는 소지금이" in result
        assert "missing from the project right now" in result
        # And it stays outstanding, so a retry that also fails says the same again.
        assert state.knowledge_deleted_unreplaced != []
        assert "NOTHING WAS RECORDED" in await record(tools)

    asyncio.run(run())


def test_the_record_cap_never_blocks_a_replacement() -> None:
    """Otherwise the budget itself becomes the thing that loses knowledge.

    The cap exists to stop a run narrating into the knowledge base. Applied to the
    second half of a repair it would refuse the only call that can put back what
    the run already deleted.
    """

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_seen["41"] = "옛 규칙"
        state.knowledge_records_attempted = MAX_RECORDS_PER_RUN

        spent = await record(tools)
        assert creates(sent) == [] and str(MAX_RECORDS_PER_RUN) in spent

        await forget(tools)
        result = await record(tools)

        assert len(creates(sent)) == 1
        assert "completes the correction" in result

    asyncio.run(run())


def test_a_successful_record_closes_the_outstanding_deletion() -> None:
    """Once the replacement is on the wire the run owes nothing, and a later
    failure must not go on naming an entry that has already been replaced."""

    async def run() -> None:
        _, state, tools, sent = make()
        state.knowledge_seen["41"] = "옛 규칙"

        await forget(tools)
        await record(tools)
        assert state.knowledge_deleted_unreplaced == []

        later = await record(tools, tag="GAMEPLAY")

        assert "NOTHING WAS RECORDED" not in later

    asyncio.run(run())


# --- ARTEL-180 again: a write must not drag a scene view back in --------------


def test_a_write_neither_touches_the_game_nor_returns_a_scene() -> None:
    """The same regression `search_knowledge` had to avoid. Neither write changes
    a screen, so returning one would re-spend the context `fold_stale_scenes` was
    written to save, and cost a game round trip for a picture already in hand."""

    async def run() -> None:
        channel, state, tools, sent = make()
        channel.on_game_state(
            {
                "payload": {
                    "scene": "Shop",
                    "interactables": [{"id": 1, "name": "Buy", "type": "button"}],
                }
            }
        )
        frames_before = channel.scene.frames
        state.knowledge_seen["41"] = "옛 규칙"

        recorded = await record(tools)
        forgotten = await forget(tools)

        for result in (recorded, forgotten):
            assert SCENE_VIEW_START_PREFIX not in result
            assert "you can act on:" not in result
        assert [f for f in sent if f["type"] == MessageType.ACTION.value] == []
        assert channel.scene.frames == frames_before

    asyncio.run(run())


def test_operator_messages_still_ride_out_on_a_write() -> None:
    """Operator words reach the run on the next tool result whatever that tool is."""

    async def run() -> None:
        channel, state, tools, _ = make()
        state.knowledge_seen["41"] = "옛 규칙"

        channel.on_chat({"payload": {"message": "지식은 그만 건드리세요"}})
        assert "지식은 그만 건드리세요" in await record(tools)

        channel.on_chat({"payload": {"message": "상점은 건너뛰세요"}})
        assert "상점은 건너뛰세요" in await forget(tools)

    asyncio.run(run())


# --- what the model is handed -------------------------------------------------


def test_the_record_description_draws_the_line_it_has_to_draw() -> None:
    """Its whole job. An agent that files this run's own state teaches later runs
    things that were true for one minute, and one that files a bug teaches them
    that the broken behaviour is correct."""
    _, _, tools, _ = make()
    description = tools["record_knowledge"].description

    assert str(MAX_RECORDS_PER_RUN) in description
    for name in KNOWLEDGE_TAGS:
        assert name in description
    # This run's own state is not knowledge.
    assert "500 gold" in description
    # A bug is reported, not recorded.
    assert "report_step" in description


def test_the_forget_description_sets_the_bar_high_and_names_the_second_half() -> None:
    """The two things that make deletion survivable: not deleting over a single
    contradiction, and never stopping between the delete and the re-record."""
    _, _, tools, _ = make()
    description = tools["forget_knowledge"].description

    assert str(MAX_FORGETS_PER_RUN) in description
    # One contradiction is more often a bug than stale documentation.
    assert "ONE contradiction is not enough" in description
    assert "report_step" in description
    # Correction is delete-then-record, in one step.
    assert "record_knowledge" in description
    assert "IMMEDIATELY" in description
    # And deleting is only possible for what the run has read.
    assert "search_knowledge" in description


# --- the whole loop -----------------------------------------------------------


class ScriptedModel(BaseChatModel):
    """Returns one scripted tool call per turn, and records what it was given."""

    turns: list[dict]
    received: list[list[BaseMessage]] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=turn.get("content", ""),
                        tool_calls=turn.get("tool_calls", []),
                    )
                )
            ]
        )


def test_a_run_searches_knowledge_and_carries_on_to_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, end to end: a real `QaRunner`, a real tool loop, a real channel.

    A tool that works when invoked directly and stalls inside the graph would
    look identical in every test above. What this pins is that the answer comes
    back as a tool result the next model turn can actually read, and that the run
    goes on to report and finish.
    """
    model = ScriptedModel(
        turns=[
            {
                "tool_calls": [
                    {
                        "name": "search_knowledge",
                        "args": {
                            "step": 1,
                            "thought": "구매 실패 규칙을 확인한다",
                            "query": "골드가 모자라면 구매 버튼이 어떻게 되나",
                            "tag": "RULE",
                        },
                        "id": "1",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "observe_scene",
                        "args": {"step": 1, "thought": "화면을 본다"},
                        "id": "2",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "report_step",
                        "args": {
                            "step": 1,
                            "passed": True,
                            "message": "문서대로 버튼이 비활성이다",
                            "thought": "판정",
                        },
                        "id": "3",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "통과", "thought": "종료"},
                        "id": "4",
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    monkeypatch.setattr("app.agents.qa.runner.build_chat_model", lambda _model: model)

    sent: list[dict] = []

    async def send(frame: dict) -> None:
        """Answer both correspondents the way they answer in production.

        Orchestration replies to a KNOWLEDGE_SEARCH; the game replies to an
        ACTION. Both resolve synchronously here so the loop needs no background
        task to make progress.
        """
        sent.append(frame)
        if frame["type"] == MessageType.KNOWLEDGE_SEARCH.value:
            channel.on_knowledge_search_result(
                {
                    "correlationId": frame["messageId"],
                    "payload": {"query": "골드", "model": "e5", "results": [hit()]},
                }
            )
            return
        if frame["type"] != MessageType.ACTION.value:
            return
        channel.on_game_state(
            {"payload": {"scene": "Shop", "interactables": [], "observables": {}}}
        )
        channel.on_action_result(
            {
                "correlationId": frame["messageId"],
                "payload": {
                    "results": [
                        {"id": a["id"], "success": True} for a in frame["payload"]["actions"]
                    ]
                },
            }
        )

    channel = QaRunChannel(qa_try_id=1, send=send)
    state = QaRunState(total_steps=1)
    scenario = ScenarioDraft(
        title="구매 실패 확인",
        description="골드가 모자랄 때 구매가 막히는지 확인한다.",
        steps=[
            ScenarioStep(
                step=1,
                title="구매 시도",
                state="상점",
                action="구매를 누른다",
                expected="구매가 되지 않는다",
            )
        ],
    )

    asyncio.run(QaRunner().run(channel, scenario, state))

    assert state.finished
    assert len(searches(sent)) == 1
    assert state.knowledge_searches_attempted == 1

    # The second model turn is the first one made after the search answered.
    knowledge_result = [
        m.content
        for m in model.received[1]
        if isinstance(m, ToolMessage) and isinstance(m.content, str)
    ]
    assert len(knowledge_result) == 1
    assert "구매는 소지금이" in knowledge_result[0]
    # ARTEL-180: the answer carried no scene, and the observation that follows is
    # what puts the first one in the transcript.
    assert SCENE_VIEW_START_PREFIX not in knowledge_result[0]


@pytest.mark.parametrize("field", ["id", "tag", "source", "summary", "description", "score"])
def test_a_hit_missing_a_field_is_still_delivered(field: str) -> None:
    """A dropped frame would leave the waiting tool to time out — the run paying
    twenty seconds for a renamed field. Every field defaults instead."""

    async def run() -> None:
        channel, _, tools, sent = make()
        partial = hit()
        partial.pop(field)

        answer(channel, sent, {"query": "q", "model": "e5", "results": [partial]})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "did not answer in time" not in result

    asyncio.run(run())


def test_a_run_corrects_an_entry_by_deleting_it_and_recording_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The correction, end to end: a real `QaRunner`, a real tool loop, a real channel.

    This is the shape the tool descriptions ask for and the only shape that does
    not lose knowledge — search, delete, record, all inside one step, before the
    verdict. What it pins beyond the individual tools is that the ids survive the
    trip: the delete quotes an id the agent could only have read off the search
    result the graph handed it a turn earlier.
    """
    model = ScriptedModel(
        turns=[
            {
                "tool_calls": [
                    {
                        "name": "search_knowledge",
                        "args": {
                            "step": 1,
                            "thought": "구매 규칙을 확인한다",
                            "query": "골드가 모자라면 구매 버튼이 어떻게 되나",
                        },
                        "id": "1",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "forget_knowledge",
                        "args": {
                            "step": 1,
                            "thought": "빌드가 반복해서 이 규칙과 어긋난다",
                            "knowledge_id": "41",
                        },
                        "id": "2",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "record_knowledge",
                        "args": {
                            "step": 1,
                            "thought": "고친 내용을 곧바로 다시 남긴다",
                            "tag": "RULE",
                            "summary": "구매는 소지금이 가격의 절반 이상이면 가능하다",
                            "description": "할인 적용 후 가격을 기준으로 판정한다.",
                        },
                        "id": "3",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "report_step",
                        "args": {
                            "step": 1,
                            "passed": True,
                            "message": "구매가 가능했다",
                            "thought": "판정",
                        },
                        "id": "4",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "통과", "thought": "종료"},
                        "id": "5",
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    monkeypatch.setattr("app.agents.qa.runner.build_chat_model", lambda _model: model)

    sent: list[dict] = []

    async def send(frame: dict) -> None:
        """Orchestration answers a search and says nothing at all to a write."""
        sent.append(frame)
        if frame["type"] == MessageType.KNOWLEDGE_SEARCH.value:
            channel.on_knowledge_search_result(
                {
                    "correlationId": frame["messageId"],
                    "payload": {"query": "골드", "model": "e5", "results": [hit(id="41")]},
                }
            )

    channel = QaRunChannel(qa_try_id=1, send=send)
    state = QaRunState(total_steps=1)
    scenario = ScenarioDraft(
        title="구매 규칙 확인",
        description="구매 조건이 문서대로인지 확인한다.",
        steps=[
            ScenarioStep(
                step=1,
                title="구매 시도",
                state="상점",
                action="구매를 누른다",
                expected="구매가 된다",
            )
        ],
    )

    asyncio.run(QaRunner().run(channel, scenario, state))

    assert state.finished
    assert deletes(sent)[0]["payload"] == {"knowledge_id": "41"}
    assert creates(sent)[0]["payload"]["summary"] == (
        "구매는 소지금이 가격의 절반 이상이면 가능하다"
    )
    # The delete came first and the record closed it: the run owes nothing.
    assert sent.index(deletes(sent)[0]) < sent.index(creates(sent)[0])
    assert state.knowledge_deleted_unreplaced == []

    # The turn after the deletion is where the agent was told to finish the repair.
    after_delete = [
        m.content
        for m in model.received[2]
        if isinstance(m, ToolMessage) and isinstance(m.content, str)
    ]
    assert "record_knowledge" in after_delete[-1]
