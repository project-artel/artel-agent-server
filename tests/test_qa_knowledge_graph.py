"""The graph tools, and the line between having read an entry and having glimpsed it.

The sharpest thing here is not any single tool. It is that neighbours arrive as
one-line summaries and must NOT count as having read the entry: `knowledge_seen`
is what `update_knowledge` and `forget_knowledge` rest on, and deletion is the
most destructive thing the agent does. Letting a 120-character line satisfy that
precondition would be the first regression this feature could ship.
"""

import asyncio

from app.agents.qa.knowledge import (
    KNOWLEDGE_RELATIONS,
    MAX_LINKS_PER_RUN,
    MAX_NEIGHBOUR_SUMMARY_CHARS,
    render_expansion,
    render_neighbour,
)
from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import (
    KnowledgeExpandResultPayload,
    KnowledgeNeighbour,
    KnowledgeSearchHit,
    KnowledgeSearchResultPayload,
    MessageType,
)


def make(total_steps: int = 1):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=0.05, write_timeout=0.05)
    state = QaRunState(total_steps=total_steps)
    tools = {tool.name: tool for tool in build_tools(channel, state)}
    return channel, state, tools, sent


def frames(sent: list[dict], message_type: MessageType) -> list[dict]:
    return [frame for frame in sent if frame["type"] == message_type.value]


def neighbour(**kwargs) -> KnowledgeNeighbour:
    base = {
        "id": "42",
        "relation": "REFINES",
        "origin": "EDGE",
        "direction": "OUT",
        "note": "이유",
        "tag": "RULE",
        "source": "QA",
        "summary": "구체적인 예외",
        "depth": 1,
        "score": None,
        "via": "1",
    }
    return KnowledgeNeighbour(**{**base, **kwargs})


def answer_search(channel: QaRunChannel, sent: list[dict], hits: list[KnowledgeSearchHit]):
    """Reply to the search currently in flight, the way Orchestration does."""

    async def reply() -> None:
        for _ in range(50):
            if frames(sent, MessageType.KNOWLEDGE_SEARCH):
                break
            await asyncio.sleep(0)
        channel.on_knowledge_search_result(
            {
                "correlationId": frames(sent, MessageType.KNOWLEDGE_SEARCH)[-1]["messageId"],
                "payload": KnowledgeSearchResultPayload(
                    query="q", model="m", results=hits
                ).model_dump(),
            }
        )

    return asyncio.create_task(reply())


def answer_expand(channel: QaRunChannel, sent: list[dict], payload: KnowledgeExpandResultPayload):
    async def reply() -> None:
        for _ in range(50):
            if frames(sent, MessageType.KNOWLEDGE_EXPAND):
                break
            await asyncio.sleep(0)
        channel.on_knowledge_expand_result(
            {
                "correlationId": frames(sent, MessageType.KNOWLEDGE_EXPAND)[-1]["messageId"],
                "payload": payload.model_dump(),
            }
        )

    return asyncio.create_task(reply())


# --- rendering ----------------------------------------------------------------


def test_a_neighbour_is_one_line_and_does_not_carry_its_note() -> None:
    """The note is the auditor's field and can run as long as the reasoning did.

    Inlined under every hit it would roughly double what an expanded search costs
    the transcript, for something `expand_knowledge` returns in full.
    """
    line = render_neighbour(neighbour(note="아주 긴 이유가 여기 들어간다"))

    assert line.count("\n") == 0
    assert "아주 긴 이유" not in line
    assert "id 42" in line
    assert "refines" in line


def test_a_long_neighbour_summary_is_clipped() -> None:
    line = render_neighbour(neighbour(summary="가" * 400))
    assert len(line) < 400
    assert "…" in line


def test_an_incoming_relation_reads_in_the_other_direction() -> None:
    """"refines" and "refined by" are different claims, and the hit is the object."""
    assert "refined by" in render_neighbour(neighbour(direction="IN"))
    assert "refines" in render_neighbour(neighbour(direction="OUT"))


def test_a_symmetric_relation_gets_no_direction_word() -> None:
    """A direction on CONTRADICTS would invent a claim the graph never made."""
    line = render_neighbour(neighbour(relation="CONTRADICTS", direction="NONE"))
    assert "contradicts" in line
    assert "contradicted by" not in line


def test_a_vector_neighbour_is_marked_apart_and_shows_its_similarity() -> None:
    """A machine guess must not read like something a run asserted."""
    guess = render_neighbour(
        neighbour(relation="SIMILAR", origin="VECTOR", direction="NONE", note=None, score=0.71)
    )
    asserted = render_neighbour(neighbour())

    assert "~" in guess and "0.71" in guess
    assert "↳" in asserted


def test_an_expansion_prints_the_notes_it_folded_away() -> None:
    """This is the call the run spent a budget slot on to see more."""
    text = render_expansion(
        KnowledgeExpandResultPayload(
            id="1",
            summary="마을 화면",
            neighbors=[neighbour(relation="LEADS_TO", note="상단바의 상점 버튼")],
        ),
        remaining=2,
    )

    assert "상단바의 상점 버튼" in text
    assert "마을 화면" in text
    assert "2 knowledge expansion(s) left" in text


def test_a_truncated_expansion_says_so() -> None:
    """A silently cut list reads as "that is all there is", which it is not."""
    text = render_expansion(
        KnowledgeExpandResultPayload(id="1", neighbors=[neighbour()], truncated=True),
        remaining=1,
    )
    assert "cut" in text


def test_an_empty_expansion_is_an_answer_not_an_error() -> None:
    text = render_expansion(KnowledgeExpandResultPayload(id="1"), remaining=1)
    assert "not an" in text and "error" in text


# --- seen vs glimpsed ---------------------------------------------------------


def test_neighbours_are_glimpsed_and_hits_are_seen() -> None:
    async def run() -> None:
        channel, state, tools, sent = make()
        answer_search(
            channel,
            sent,
            [KnowledgeSearchHit(id="1", summary="히트", neighbors=[neighbour(id="42")])],
        )
        await tools["search_knowledge"].ainvoke({"step": 1, "thought": "t", "query": "q"})

        assert "1" in state.knowledge_seen
        assert "42" in state.knowledge_glimpsed
        assert "42" not in state.knowledge_seen

    asyncio.run(run())


def test_a_neighbour_line_appears_under_its_hit() -> None:
    async def run() -> None:
        channel, _, tools, sent = make()
        answer_search(
            channel,
            sent,
            [KnowledgeSearchHit(id="1", summary="히트", neighbors=[neighbour(summary="이웃 요약")])],
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "t", "query": "q"}
        )

        assert "이웃 요약" in result

    asyncio.run(run())


def test_deleting_something_only_glimpsed_is_refused_with_what_to_do() -> None:
    """The regression this feature could most easily ship.

    `FORGET_KNOWLEDGE_DESCRIPTION` calls deletion the most destructive thing the
    agent does; a clipped one-line summary is not having read the entry. The
    message has to name the reason, or the agent meets a refusal it cannot
    explain — it can see the id right there in the transcript.
    """

    async def run() -> None:
        channel, state, tools, sent = make()
        answer_search(
            channel, sent, [KnowledgeSearchHit(id="1", neighbors=[neighbour(id="42")])]
        )
        await tools["search_knowledge"].ainvoke({"step": 1, "thought": "t", "query": "q"})

        result = await tools["forget_knowledge"].ainvoke(
            {"step": 1, "thought": "t", "knowledge_id": "42"}
        )

        assert "Nothing was deleted" in result
        assert "neighbour line" in result
        assert not frames(sent, MessageType.KNOWLEDGE_DELETE)

    asyncio.run(run())


def test_correcting_something_only_glimpsed_is_refused_too() -> None:
    async def run() -> None:
        channel, _, tools, sent = make()
        answer_search(
            channel, sent, [KnowledgeSearchHit(id="1", neighbors=[neighbour(id="42")])]
        )
        await tools["search_knowledge"].ainvoke({"step": 1, "thought": "t", "query": "q"})

        result = await tools["update_knowledge"].ainvoke(
            {"step": 1, "thought": "t", "knowledge_id": "42", "summary": "새 요약"}
        )

        assert "neighbour line" in result
        assert not frames(sent, MessageType.KNOWLEDGE_UPDATE)

    asyncio.run(run())


def test_linking_accepts_an_endpoint_that_was_only_glimpsed() -> None:
    """Asserting a relation destroys nothing, and a summary is enough to justify one."""

    async def run() -> None:
        channel, _, tools, sent = make()
        answer_search(
            channel, sent, [KnowledgeSearchHit(id="1", neighbors=[neighbour(id="42")])]
        )
        await tools["search_knowledge"].ainvoke({"step": 1, "thought": "t", "query": "q"})

        result = await tools["link_knowledge"].ainvoke(
            {
                "step": 1,
                "thought": "t",
                "from_knowledge_id": "42",
                "to_knowledge_id": "1",
                "relation": "REFINES",
                "note": "상점 화면에서 확인",
            }
        )

        assert "Sent" in result
        assert frames(sent, MessageType.KNOWLEDGE_LINK)

    asyncio.run(run())


# --- link -------------------------------------------------------------------


def seen(state: QaRunState, *ids: str) -> None:
    for entry_id in ids:
        state.knowledge_seen[entry_id] = "요약"


def test_link_goes_out_with_the_relation_and_note() -> None:
    async def run() -> None:
        _, state, tools, sent = make()
        seen(state, "1", "2")

        await tools["link_knowledge"].ainvoke(
            {
                "step": 1,
                "thought": "경로를 적는다",
                "from_knowledge_id": "1",
                "to_knowledge_id": "2",
                "relation": "leads_to",
                "note": "상단바의 상점 버튼",
            }
        )

        payload = frames(sent, MessageType.KNOWLEDGE_LINK)[0]["payload"]
        assert payload["relation"] == "LEADS_TO"
        assert payload["note"] == "상단바의 상점 버튼"
        assert payload["from_knowledge_id"] == "1"

    asyncio.run(run())


def test_link_refuses_locally_what_orchestration_would_reject_in_silence() -> None:
    """A link is one-way, so a rejection there is invisible here.

    Every one of these would be reported to the model as a success if the check
    lived only on the far side.
    """

    async def run() -> None:
        _, state, tools, sent = make()
        seen(state, "1", "2")

        bad_relation = await tools["link_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "2", "relation": "RELATED_TO", "note": "n",
            }
        )
        blank_note = await tools["link_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "2", "relation": "REFINES", "note": "   ",
            }
        )
        self_link = await tools["link_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "1", "relation": "REFINES", "note": "n",
            }
        )
        unseen = await tools["link_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "999", "relation": "REFINES", "note": "n",
            }
        )

        assert "not a knowledge relation" in bad_relation
        assert "`note` is required" in blank_note
        assert "cannot be related to itself" in self_link
        assert "has been shown" in unseen
        assert not frames(sent, MessageType.KNOWLEDGE_LINK)

    asyncio.run(run())


def test_link_budget_is_separate_from_unlink() -> None:
    """A run that spent all its links must still be able to withdraw a wrong one."""

    async def run() -> None:
        _, state, tools, sent = make()
        seen(state, "1", "2")
        state.knowledge_links_attempted = MAX_LINKS_PER_RUN

        refused = await tools["link_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "2", "relation": "REFINES", "note": "n",
            }
        )
        allowed = await tools["unlink_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "2", "relation": "REFINES",
            }
        )

        assert "used all" in refused
        assert "Sent" in allowed

    asyncio.run(run())


def test_unlink_names_the_relation_not_an_edge_id() -> None:
    """The agent has never seen an edge id — the id on a neighbour is a knowledge id."""

    async def run() -> None:
        _, state, tools, sent = make()
        seen(state, "1", "2")

        await tools["unlink_knowledge"].ainvoke(
            {
                "step": 1, "thought": "경로가 없었다",
                "from_knowledge_id": "1", "to_knowledge_id": "2", "relation": "LEADS_TO",
            }
        )

        payload = frames(sent, MessageType.KNOWLEDGE_UNLINK)[0]["payload"]
        assert payload == {
            "from_knowledge_id": "1",
            "to_knowledge_id": "2",
            "relation": "LEADS_TO",
        }

    asyncio.run(run())


def test_a_dead_socket_on_a_link_does_not_end_the_run() -> None:
    """The frame failing must be reported, not raised out of the tool.

    Only the LINK frame is made to fail. The thought row goes out first and
    through the same socket, and it is deliberately NOT guarded — that is how
    every other knowledge tool here behaves, and widening the guard would change
    those too under the cover of this change.
    """

    async def run() -> None:
        channel, state, tools, sent = make()
        seen(state, "1", "2")
        original = channel._send  # noqa: SLF001

        async def boom(frame: dict) -> None:
            if frame["type"] == MessageType.KNOWLEDGE_LINK.value:
                raise RuntimeError("소켓 죽음")
            await original(frame)

        channel._send = boom  # noqa: SLF001 - the failure has to come from the transport
        result = await tools["link_knowledge"].ainvoke(
            {
                "step": 1, "thought": "t", "from_knowledge_id": "1",
                "to_knowledge_id": "2", "relation": "REFINES", "note": "n",
            }
        )

        assert "could not be sent" in result

    asyncio.run(run())


# --- expand -------------------------------------------------------------------


def test_expand_returns_the_neighbourhood_and_remembers_it_as_glimpsed() -> None:
    async def run() -> None:
        channel, state, tools, sent = make()
        seen(state, "1")
        answer_expand(
            channel,
            sent,
            KnowledgeExpandResultPayload(
                id="1", summary="마을", neighbors=[neighbour(id="42", summary="상점")]
            ),
        )

        result = await tools["expand_knowledge"].ainvoke(
            {"step": 1, "thought": "더 본다", "knowledge_id": "1"}
        )

        assert "상점" in result
        assert "42" in state.knowledge_glimpsed
        assert "42" not in state.knowledge_seen

    asyncio.run(run())


def test_expand_clamps_the_depth_it_asks_for() -> None:
    async def run() -> None:
        channel, state, tools, sent = make()
        seen(state, "1")
        answer_expand(channel, sent, KnowledgeExpandResultPayload(id="1"))

        await tools["expand_knowledge"].ainvoke(
            {"step": 1, "thought": "t", "knowledge_id": "1", "depth": 9}
        )

        assert frames(sent, MessageType.KNOWLEDGE_EXPAND)[0]["payload"]["depth"] == 2

    asyncio.run(run())


def test_expand_refuses_an_id_this_run_has_not_been_shown() -> None:
    async def run() -> None:
        _, _, tools, sent = make()
        result = await tools["expand_knowledge"].ainvoke(
            {"step": 1, "thought": "t", "knowledge_id": "999"}
        )
        assert "has been shown" in result
        assert not frames(sent, MessageType.KNOWLEDGE_EXPAND)

    asyncio.run(run())


def test_expand_that_times_out_is_not_a_failed_step() -> None:
    """None of timeout, refusal or empty answer is a reason to stop judging."""

    async def run() -> None:
        _, state, tools, _ = make()
        seen(state, "1")
        # Nobody answers; the channel's own timeout is what returns.
        from app.qa import channel as channel_module

        original = channel_module.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS
        channel_module.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS = 0.01
        try:
            result = await tools["expand_knowledge"].ainvoke(
                {"step": 1, "thought": "t", "knowledge_id": "1"}
            )
        finally:
            channel_module.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS = original

        assert "did not answer in time" in result

    asyncio.run(run())


def test_an_error_frame_releases_the_expansion_not_the_search() -> None:
    """Two waiters, and a reply must resolve only the one it answers."""

    async def run() -> None:
        channel, state, tools, sent = make()
        seen(state, "1")

        async def reply() -> None:
            for _ in range(50):
                if frames(sent, MessageType.KNOWLEDGE_EXPAND):
                    break
                await asyncio.sleep(0)
            channel.on_error(
                {
                    "correlationId": frames(sent, MessageType.KNOWLEDGE_EXPAND)[-1]["messageId"],
                    "payload": {"message": "확장 실패"},
                }
            )

        asyncio.create_task(reply())
        result = await tools["expand_knowledge"].ainvoke(
            {"step": 1, "thought": "t", "knowledge_id": "1"}
        )

        assert "could not run" in result
        assert "확장 실패" in result

    asyncio.run(run())


def test_the_relation_vocabulary_has_no_catch_all() -> None:
    """An agent with one easy option and four hard ones picks the easy one."""
    assert "RELATED_TO" not in KNOWLEDGE_RELATIONS
    assert "SEE_ALSO" not in KNOWLEDGE_RELATIONS
    # SIMILAR is a display label; sending it would be rejected by Orchestration's CHECK.
    assert "SIMILAR" not in KNOWLEDGE_RELATIONS
    assert "LEADS_TO" in KNOWLEDGE_RELATIONS


def test_the_neighbour_clip_is_small_enough_to_stay_a_handful_of_lines() -> None:
    """Eight neighbours at this width is the budget ARTEL-275 sized the caps for."""
    assert MAX_NEIGHBOUR_SUMMARY_CHARS <= 160


def test_citing_something_only_glimpsed_is_allowed() -> None:
    """The mirror of the two refusals above, and the reason `knows_of` exists.

    `update_knowledge` and `forget_knowledge` demand `knowledge_seen` because they
    destroy something. A citation destroys nothing — it records that a line the
    run was shown is what changed its mind — so the bar is `knows_of`, and an
    entry met only as a neighbour line clears it.
    """

    async def run() -> None:
        channel, state, tools, sent = make(total_steps=1)

        task = answer_search(
            channel, sent, [KnowledgeSearchHit(id="1", neighbors=[neighbour(id="42")])]
        )
        await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "찾아본다", "query": "상점"}
        )
        await task
        assert "42" not in state.knowledge_seen and "42" in state.knowledge_glimpsed

        await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "이웃 줄이 말한 대로였다",
                "thought": "그 한 줄이 판정을 바꿨다",
                "used_knowledge_ids": ["42"],
            }
        )

        payload = frames(sent, MessageType.STATUS)[0]["payload"]
        assert payload["used_knowledge_ids"] == ["42"]
        assert payload["rejected_knowledge_id_count"] == 0

    asyncio.run(run())


def test_an_expansion_says_which_step_asked() -> None:
    """The expansion writes usage rows too, so it carries the same coordinate."""

    async def run() -> None:
        channel, state, tools, sent = make(total_steps=1)
        state.knowledge_seen["1"] = "마을"

        task = answer_expand(
            channel, sent, KnowledgeExpandResultPayload(id="1", neighbors=[neighbour()])
        )
        await tools["expand_knowledge"].ainvoke(
            {"step": 4, "thought": "관계를 따라간다", "knowledge_id": "1"}
        )
        await task

        assert frames(sent, MessageType.KNOWLEDGE_EXPAND)[0]["payload"]["step"] == 4

    asyncio.run(run())
