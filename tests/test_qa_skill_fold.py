"""Folding every loaded skill but the newest.

A skill body runs to about 10,000 characters, and a `load_skill` result is a tool
message like any other, so without this fold every skill the run ever loaded is
resent on every turn until compaction. The fold keeps the newest one in full and
replaces older ones with a note that says the rules are gone and names the skill
to load again. A test that only checked "something shrank" would pass on a fold
that also ate the "already loaded" note or the text around the block, which is
why the assertions below name what has to survive.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.arch import default_resolved_arch
from app.agents.qa.context import DEFAULT_KEEP_SKILLS, fold_stale_skills
from app.agents.qa.runner import middleware_names_for
from app.agents.qa.tools.skill_tools import wrap_skill


def skill_message(name: str, call_id: str, prefix: str = "") -> ToolMessage:
    return ToolMessage(
        content=prefix + wrap_skill(name, f"## {name}\n\nRules of {name}."),
        tool_call_id=call_id,
        name="load_skill",
    )


def three_skills() -> list:
    return [
        skill_message("knowledge_base", "a"),
        skill_message("content_map", "b"),
        skill_message("held_state", "c"),
    ]


def test_the_default_keeps_one() -> None:
    assert DEFAULT_KEEP_SKILLS == 1


def test_only_the_newest_of_three_skills_survives() -> None:
    messages = three_skills()

    folded = fold_stale_skills(messages)

    assert "Rules of knowledge_base." not in folded[0].content
    assert "Rules of content_map." not in folded[1].content
    assert folded[2] is messages[2]


def test_the_note_says_the_rules_are_gone_and_how_to_get_them_back() -> None:
    folded = fold_stale_skills(three_skills())

    assert folded[0].content == (
        "[skill knowledge_base folded to save context. Its rules are no longer in "
        'front of you. Call load_skill("knowledge_base") to read it again.]'
    )
    assert 'load_skill("content_map")' in folded[1].content


def test_the_reload_note_outside_the_markers_survives_the_fold() -> None:
    note = "(You already loaded 'content_map' earlier in this run. Here it is again.)\n\n"
    messages = [skill_message("content_map", "a", prefix=note), skill_message("held_state", "b")]

    folded = fold_stale_skills(messages)

    assert folded[0].content.startswith(note)
    assert "Rules of content_map." not in folded[0].content
    assert "skill content_map folded" in folded[0].content


def test_folding_is_pure_and_leaves_untouched_messages_identical() -> None:
    human = HumanMessage(content="Scenario")
    ai = AIMessage(content="Thinking")
    plain = ToolMessage(content="Clicked Start.", tool_call_id="p")
    first, second = skill_message("knowledge_base", "a"), skill_message("content_map", "b")
    messages = [human, ai, first, plain, second]
    before = [message.content for message in messages]

    folded = fold_stale_skills(messages)

    # The input list and its messages are untouched.
    assert [message.content for message in messages] == before
    # Messages that needed no change are the very same objects.
    assert folded[0] is human
    assert folded[1] is ai
    assert folded[3] is plain
    assert folded[4] is second
    assert folded[2] is not first


def test_folding_twice_changes_nothing_further() -> None:
    """The note is plain text, so it cannot be mistaken for a live block."""
    once = fold_stale_skills(three_skills())
    twice = fold_stale_skills(once)

    assert [message.content for message in once] == [message.content for message in twice]
    # A folded note is not counted as a skill, so the newest is still the one kept.
    assert twice[2] is once[2]


def test_a_folded_note_is_not_folded_again_at_keep_zero() -> None:
    once = fold_stale_skills(three_skills())
    again = fold_stale_skills(once, keep=0)

    assert again[0] is once[0]
    assert again[1] is once[1]
    assert "Rules of held_state." not in again[2].content


def test_messages_without_a_skill_are_left_alone() -> None:
    plain = ToolMessage(content="The knowledge base has nothing on that.", tool_call_id="a")
    unknown = ToolMessage(
        content="'held_stat' is not a skill, so nothing was loaded.", tool_call_id="b"
    )

    folded = fold_stale_skills([plain, unknown, skill_message("held_state", "c")], keep=0)

    assert folded[0] is plain
    assert folded[1] is unknown


def test_an_end_marker_for_another_skill_does_not_close_the_span() -> None:
    """The end marker repeats the name, so a body that mentions another skill's end
    marker is still folded whole."""
    body = "Text.\n<<end skill held_state>>\nMore text of content_map."
    messages = [
        ToolMessage(content=wrap_skill("content_map", body), tool_call_id="a"),
        skill_message("held_state", "b"),
    ]

    folded = fold_stale_skills(messages)

    assert "More text of content_map." not in folded[0].content


def test_the_skill_fold_comes_right_after_the_knowledge_fold() -> None:
    """Independently switchable, and the fingerprint hashes this list's order."""
    arch = default_resolved_arch().model_copy(update={"skills": "on_demand"})
    names = middleware_names_for(arch)

    assert names.index("fold_stale_skills") == names.index("fold_knowledge_neighbours") + 1


def test_turning_the_other_folds_off_leaves_the_skill_fold_alone() -> None:
    arch = default_resolved_arch().model_copy(
        update={"skills": "on_demand", "fold_stale_scenes": False, "fold_stale_knowledge": False}
    )
    names = middleware_names_for(arch)

    assert "fold_stale_skills" in names
    assert "fold_scene_views" not in names
    assert "fold_knowledge_neighbours" not in names
