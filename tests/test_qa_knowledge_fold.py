"""Folding the neighbour blocks a search volunteered — and nothing else.

The line this pins is the whole design. `fold_stale_scenes` folds a scene
because the game moved on and `observe_scene` gets it back for nothing. A
knowledge description is not stale, and getting it back costs a search out of a
budget of six — folding it would tell the agent to spend a scarce resource
undoing the fold. The neighbour block is the opposite on both counts: never
asked for, and exactly recoverable by `expand_knowledge` out of its own budget.

So: neighbours fold, hits do not. A test that only checked "something shrank"
would pass on a fold that ate the hit bodies too, which is why the assertions
below name what has to survive.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.arch import default_resolved_arch
from app.agents.qa.context import fold_stale_knowledge
from app.agents.qa.knowledge import render_results
from app.agents.qa.runner import middleware_names_for
from app.qa.envelope import (
    KnowledgeNeighbour,
    KnowledgeSearchHit,
    KnowledgeSearchResultPayload,
)


def neighbour(**kwargs) -> KnowledgeNeighbour:
    base = {
        "id": "42",
        "relation": "REFINES",
        "origin": "EDGE",
        "direction": "OUT",
        "note": "이유",
        "tag": "RULE",
        "source": "QA",
        "summary": "이웃 요약",
        "depth": 1,
        "score": None,
        "via": "1",
    }
    return KnowledgeNeighbour(**{**base, **kwargs})


def search_result(hit_id: str, summary: str, neighbour_id: str) -> str:
    """The text one `search_knowledge` call actually returns."""
    return render_results(
        KnowledgeSearchResultPayload(
            query="q",
            model="m",
            results=[
                KnowledgeSearchHit(
                    id=hit_id,
                    tag="RULE",
                    source="DOCS",
                    summary=summary,
                    description="히트의 본문 설명",
                    score=0.9,
                    neighbors=[neighbour(id=neighbour_id)],
                )
            ],
        ),
        remaining=3,
    )


def tool_message(text: str, call_id: str = "c1") -> ToolMessage:
    return ToolMessage(content=text, tool_call_id=call_id)


def test_the_older_block_folds_and_the_newest_survives() -> None:
    messages = [
        tool_message(search_result("1", "옛 히트", "10"), "a"),
        tool_message(search_result("2", "새 히트", "20"), "b"),
    ]

    folded = fold_stale_knowledge(messages)

    assert "이웃 요약" not in folded[0].content
    assert "expand_knowledge on 1" in folded[0].content
    assert "이웃 요약" in folded[1].content


def test_the_hit_itself_is_never_touched() -> None:
    """The point of the whole design. Re-reading a hit costs a search; the run has six."""
    messages = [
        tool_message(search_result("1", "옛 히트", "10"), "a"),
        tool_message(search_result("2", "새 히트", "20"), "b"),
    ]

    folded = fold_stale_knowledge(messages)

    assert "옛 히트" in folded[0].content
    assert "히트의 본문 설명" in folded[0].content
    assert "id 1" in folded[0].content


def test_the_placeholder_names_the_entry_to_expand() -> None:
    """Unlike a folded scene, this cannot be recovered by a tool that takes no argument."""
    messages = [
        tool_message(search_result("77", "옛", "10"), "a"),
        tool_message(search_result("2", "새", "20"), "b"),
    ]

    folded = fold_stale_knowledge(messages)

    assert "neighbours of 77 folded" in folded[0].content
    assert "expand_knowledge on 77" in folded[0].content


def test_folding_is_pure_and_leaves_untouched_messages_identical() -> None:
    first = tool_message(search_result("1", "옛", "10"), "a")
    second = tool_message(search_result("2", "새", "20"), "b")
    human = HumanMessage(content="시나리오")
    ai = AIMessage(content="생각")
    messages = [human, ai, first, second]
    before = [message.content for message in messages]

    folded = fold_stale_knowledge(messages)

    # The input list and its messages are untouched.
    assert [message.content for message in messages] == before
    # Messages that needed no change are the very same objects.
    assert folded[0] is human
    assert folded[1] is ai
    assert folded[3] is second
    assert folded[2] is not first


def test_folding_twice_changes_nothing_further() -> None:
    """The placeholder is plain text, so it cannot be mistaken for a live block."""
    messages = [
        tool_message(search_result("1", "옛", "10"), "a"),
        tool_message(search_result("2", "새", "20"), "b"),
    ]

    once = fold_stale_knowledge(messages)
    twice = fold_stale_knowledge(once)

    assert [message.content for message in once] == [message.content for message in twice]


def test_messages_without_neighbours_are_left_alone() -> None:
    """A search that found nothing, an action result, anything else."""
    plain = tool_message("The knowledge base has nothing on that.", "a")
    empty_hit = tool_message(
        render_results(
            KnowledgeSearchResultPayload(
                query="q",
                model="m",
                results=[KnowledgeSearchHit(id="1", summary="이웃 없는 히트", description="본문")],
            ),
            remaining=1,
        ),
        "b",
    )

    folded = fold_stale_knowledge([plain, empty_hit, tool_message(search_result("9", "새", "90"), "c")])

    assert folded[0] is plain
    assert folded[1] is empty_hit


def test_keep_zero_folds_everything() -> None:
    messages = [tool_message(search_result("1", "히트", "10"), "a")]
    folded = fold_stale_knowledge(messages, keep=0)
    assert "이웃 요약" not in folded[0].content
    assert "히트" in folded[0].content


def test_the_two_folds_are_separate_middleware_in_a_fixed_order() -> None:
    """Independently switchable, and the fingerprint hashes this list's order."""
    arch = default_resolved_arch()
    names = middleware_names_for(arch)

    assert "fold_scene_views" in names
    assert "fold_knowledge_neighbours" in names
    assert names.index("fold_knowledge_neighbours") == names.index("fold_scene_views") + 1


def test_turning_the_knowledge_fold_off_leaves_the_scene_fold_alone() -> None:
    arch = default_resolved_arch().model_copy(update={"fold_stale_knowledge": False})
    names = middleware_names_for(arch)

    assert "fold_knowledge_neighbours" not in names
    assert "fold_scene_views" in names
