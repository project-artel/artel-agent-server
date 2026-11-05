"""Folding every kind of stale block in one batch.

The per-kind tests (`test_qa_agents_context.py`, `test_qa_pulse_fold.py`,
`test_qa_knowledge_fold.py`, `test_qa_skill_fold.py`, `test_qa_capture.py`) pin what one
kind's fold leaves behind. This file pins what only the shared batch can get wrong: that a
stale block of one kind waits for the others instead of folding on its own, that one call
folds every kind at once, and that the calls between batches send the same prefix — the
reason the four folds were merged.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.context import (
    IMAGE_SCORE_CHARS,
    FoldKind,
    fold_context,
)
from app.agents.qa.tools.skill_tools import wrap_skill
from app.agents.qa.vision import build_capture_message
from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX, SCENE_VIEW_START_SUFFIX

ALL_KINDS = frozenset(FoldKind)


def scene_view(at: int, padding: int = 0) -> str:
    return (
        f"{SCENE_VIEW_START_PREFIX}{at}{SCENE_VIEW_START_SUFFIX}\n"
        f"scene: Lobby\n{'x' * padding}\n{SCENE_VIEW_END}"
    )


def tool_result(content: str, call_id: str, name: str = "observe_scene") -> list:
    return [
        AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}]),
        ToolMessage(content=content, tool_call_id=call_id, name=name),
    ]


def skill(name: str, call_id: str) -> list:
    return tool_result(wrap_skill(name, f"Rules of {name}."), call_id, name="load_skill")


def capture(capture_id: str) -> HumanMessage:
    return build_capture_message(capture_id, "aGVsbG8=", "image/png", "This is the screen")


def stale_chars_of(messages: list, kinds=ALL_KINDS) -> int:
    """The score of the stale blocks: the largest threshold that still folds them.

    Found by search over `fold_context` itself rather than recomputed here, so the test
    does not restate how a block is scored.
    """
    lowest_that_folds_nothing = 10**7
    assert not fold_context(messages, kinds, threshold_chars=lowest_that_folds_nothing).folded_keys
    highest_that_folds = 0
    assert fold_context(messages, kinds, threshold_chars=highest_that_folds).folded_keys
    while lowest_that_folds_nothing - highest_that_folds > 1:
        middle = (lowest_that_folds_nothing + highest_that_folds) // 2
        if fold_context(messages, kinds, threshold_chars=middle).folded_keys:
            highest_that_folds = middle
        else:
            lowest_that_folds_nothing = middle
    return highest_that_folds


def two_skills_and_views() -> list:
    return [
        HumanMessage(content="Begin."),
        *skill("knowledge_base", "s1"),
        *tool_result(scene_view(1, padding=500), "a"),
        *skill("content_map", "s2"),
        *tool_result(scene_view(2, padding=500), "b"),
    ]


def test_a_stale_skill_waits_for_the_batch() -> None:
    messages = two_skills_and_views()

    fold = fold_context(messages, ALL_KINDS, threshold_chars=10**9)

    assert fold.messages == messages
    assert all(after is before for after, before in zip(fold.messages, messages))
    assert fold.newly_folded_kinds == frozenset()


def test_one_call_folds_every_kind_at_once() -> None:
    messages = two_skills_and_views()

    fold = fold_context(messages, ALL_KINDS, threshold_chars=stale_chars_of(messages))

    assert "Rules of knowledge_base." not in fold.messages[2].content
    assert 'load_skill("knowledge_base")' in fold.messages[2].content
    assert "x" * 500 not in fold.messages[4].content
    assert fold.messages[6] is messages[6]
    assert fold.messages[8] is messages[8]
    assert fold.newly_folded_kinds == {FoldKind.skill, FoldKind.scene_view}


def test_one_short_of_the_threshold_folds_nothing() -> None:
    messages = two_skills_and_views()

    fold = fold_context(messages, ALL_KINDS, threshold_chars=stale_chars_of(messages) + 1)

    assert fold.folded_keys == frozenset()
    assert fold.messages == messages


def test_between_batches_the_prefix_a_call_sent_is_sent_again() -> None:
    messages = two_skills_and_views()
    first = fold_context(messages, ALL_KINDS, threshold_chars=stale_chars_of(messages))

    later = [*messages, *tool_result(scene_view(3, padding=500), "c"), *skill("held_state", "s3")]
    second = fold_context(later, ALL_KINDS, first.folded_keys, threshold_chars=10**9)

    sent_before = [message.content for message in first.messages]
    assert [message.content for message in second.messages[: len(messages)]] == sent_before
    assert second.newly_folded_kinds == frozenset()


def test_the_next_batch_folds_what_went_stale_since() -> None:
    messages = two_skills_and_views()
    first = fold_context(messages, ALL_KINDS, threshold_chars=stale_chars_of(messages))
    later = [*messages, *tool_result(scene_view(3, padding=500), "c"), *skill("held_state", "s3")]

    second = fold_context(later, ALL_KINDS, first.folded_keys, threshold_chars=1)

    assert "Rules of content_map." not in second.messages[6].content
    assert "x" * 500 not in second.messages[8].content
    assert second.newly_folded_kinds == {FoldKind.skill, FoldKind.scene_view}
    assert first.folded_keys < second.folded_keys


def test_a_folded_block_stays_folded_after_compaction_drops_the_messages_before_it() -> None:
    """The key is the tool call, not the position, so a shorter list keeps its folds."""
    messages = two_skills_and_views()
    first = fold_context(messages, ALL_KINDS, threshold_chars=stale_chars_of(messages))

    summary = HumanMessage(content="Summary of the run so far.")
    compacted = [summary, *messages[3:]]
    second = fold_context(compacted, ALL_KINDS, first.folded_keys, threshold_chars=10**9)

    assert second.messages[2].content == first.messages[4].content
    assert second.newly_folded_kinds == frozenset()


def test_an_image_scores_its_fixed_size_and_folds_in_the_same_batch() -> None:
    messages = [
        HumanMessage(content="Begin."),
        capture("c1"),
        *skill("knowledge_base", "s1"),
        capture("c2"),
        capture("c3"),
        *skill("content_map", "s2"),
    ]
    skill_chars = stale_chars_of(messages, [FoldKind.skill])

    short = fold_context(messages, ALL_KINDS, threshold_chars=skill_chars + IMAGE_SCORE_CHARS + 1)
    assert short.folded_keys == frozenset()

    fold = fold_context(messages, ALL_KINDS, threshold_chars=skill_chars + IMAGE_SCORE_CHARS)
    assert isinstance(fold.messages[1].content, str)
    assert "image dropped" in fold.messages[1].content
    assert fold.messages[4] is messages[4]
    assert fold.messages[5] is messages[5]
    assert fold.newly_folded_kinds == {FoldKind.image, FoldKind.skill}


def test_a_kind_left_out_is_neither_scored_nor_folded() -> None:
    messages = two_skills_and_views()
    scene_chars = stale_chars_of(messages, [FoldKind.scene_view])

    fold = fold_context(messages, [FoldKind.scene_view], threshold_chars=scene_chars)

    assert fold.messages[2] is messages[2]
    assert fold.newly_folded_kinds == {FoldKind.scene_view}
