"""`fold_stale_scenes` collapses old scene views out of what the model reads.

Every acting tool and `observe_scene` append a full `SceneMemory.render` view to
their tool result, and every tool message stays in the conversation forever — a
run of even a handful of steps piles up dozens of near-identical dumps by the
time it matters most. These pin that only the newest views survive in full, that
everything besides the view (action outcomes, the operator block) rides through
untouched, and that the fold does not grow the input linearly with run length.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.context import DEFAULT_KEEP_SCENES, fold_stale_scenes
from app.qa.envelope import GameState, Interactable
from app.qa.scene import SceneMemory


def render_at(observation: int, scene: str = "Lobby") -> str:
    """A real, marker-wrapped view at a given observation number.

    Carries enough interactables that the view is meaningfully larger than a
    folded placeholder — a screen with one button would make folding look like
    it saves nothing, when the point is that a real scene's actionable list
    does not shrink just because this function collapses old copies of it.
    """
    interactables = [
        Interactable(id=i, name=f"Button{i}", type="button", label=f"버튼{i}")
        for i in range(1, 21)
    ]
    memory = SceneMemory()
    for _ in range(observation):
        memory.apply(
            GameState(
                scene=scene,
                interactables=interactables,
                observables={},
            )
        )
    return memory.render(0)


def tool_message(observation: int, *, tool_call_id: str | None = None) -> ToolMessage:
    return ToolMessage(content=render_at(observation), tool_call_id=tool_call_id or str(observation))


def test_only_the_newest_keep_views_survive_in_full() -> None:
    messages = [tool_message(1), tool_message(2), tool_message(3), tool_message(4)]

    folded = fold_stale_scenes(messages, keep=2)

    assert "scene: Lobby  (observation 1)" not in folded[0].content
    assert "scene: Lobby  (observation 2)" not in folded[1].content
    assert "scene: Lobby  (observation 3)" in folded[2].content
    assert "scene: Lobby  (observation 4)" in folded[3].content


def test_older_views_become_an_actionable_placeholder() -> None:
    folded = fold_stale_scenes([tool_message(1), tool_message(2), tool_message(3)], keep=1)

    placeholder = folded[0].content
    assert "observation 1" in placeholder
    assert "stale" in placeholder or "moved on" in placeholder
    assert "observe_scene" in placeholder
    # The whole view is gone, not clipped mid-list.
    assert "you can act on:" not in placeholder
    assert "[1] Start (button)" not in placeholder


def test_default_keep_matches_the_module_constant() -> None:
    messages = [tool_message(1), tool_message(2), tool_message(3)]

    folded_default = fold_stale_scenes(messages)
    folded_explicit = fold_stale_scenes(messages, keep=DEFAULT_KEEP_SCENES)

    assert [m.content for m in folded_default] == [m.content for m in folded_explicit]


def test_action_outcome_lines_survive_folding() -> None:
    """`_run` in app/agents/qa/tools.py puts these above the view."""
    content = "  button_click: ok\n\n" + render_at(1)
    messages = [ToolMessage(content=content, tool_call_id="1"), tool_message(2)]

    folded = fold_stale_scenes(messages, keep=1)

    assert folded[0].content.startswith("  button_click: ok\n\n")
    assert "you can act on:" not in folded[0].content


def test_operator_block_survives_folding() -> None:
    """`with_operator_messages` in app/qa/channel.py appends this below the view."""
    content = (
        render_at(1) + "\n\nThe operator said, and it applies from now on:\n"
        "  - 체력바부터 확인해줘"
    )
    messages = [ToolMessage(content=content, tool_call_id="1"), tool_message(2)]

    folded = fold_stale_scenes(messages, keep=1)

    assert folded[0].content.endswith(
        "The operator said, and it applies from now on:\n  - 체력바부터 확인해줘"
    )
    assert "you can act on:" not in folded[0].content


def test_a_message_with_no_scene_view_is_returned_unchanged() -> None:
    plain = ToolMessage(content="The game did not answer.", tool_call_id="1")

    folded = fold_stale_scenes([plain, tool_message(1)], keep=1)

    # Same object, not just equal content: nothing needed to change.
    assert folded[0] is plain


def test_non_tool_messages_pass_through_untouched() -> None:
    human = HumanMessage(content="Begin.")
    ai = AIMessage(content="Looking at the scene now.")
    messages = [human, ai, tool_message(1), tool_message(2), tool_message(3)]

    folded = fold_stale_scenes(messages, keep=1)

    assert folded[0] is human
    assert folded[1] is ai


def test_folding_is_idempotent() -> None:
    messages = [tool_message(1), tool_message(2), tool_message(3), tool_message(4)]

    once = fold_stale_scenes(messages, keep=2)
    twice = fold_stale_scenes(once, keep=2)

    assert [m.content for m in once] == [m.content for m in twice]


def test_folded_placeholder_does_not_look_like_a_fresh_view() -> None:
    """A folded message must not be mistaken for a new view on a second pass —
    otherwise raising `keep` later, or folding twice, could uncover text that
    reads as live when the scene has already moved on."""
    folded = fold_stale_scenes([tool_message(1), tool_message(2)], keep=2)
    refolded = fold_stale_scenes(folded, keep=0)

    # Nothing in the first fold's output should be foldable again — it already
    # got the treatment. keep=0 exposes anything the pattern would still catch.
    stale_only = fold_stale_scenes([tool_message(1), tool_message(2)], keep=0)
    assert [m.content for m in refolded] == [m.content for m in stale_only]


def test_accumulated_length_stops_growing_linearly_with_more_tool_messages() -> None:
    """The regression this whole feature exists to prevent.

    Unfolded, each extra tool message adds another whole view's worth of text —
    total length grows linearly with the number of tool calls in the run.
    Folded, every message past `keep` is replaced by a fixed-size placeholder,
    so the growth per extra message drops to a small constant regardless of how
    large the view itself is.
    """

    def total_length(n: int, *, fold: bool) -> int:
        messages = [tool_message(i) for i in range(1, n + 1)]
        if fold:
            messages = fold_stale_scenes(messages, keep=DEFAULT_KEEP_SCENES)
        return sum(len(m.content) for m in messages)

    unfolded_growth = (total_length(200, fold=False) - total_length(5, fold=False)) / (200 - 5)
    folded_growth = (total_length(200, fold=True) - total_length(5, fold=True)) / (200 - 5)

    assert folded_growth < unfolded_growth / 4
