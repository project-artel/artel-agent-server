from app.qa.envelope import ActionRecord, GameState, Interactable, Rect, Screen, Visual
from app.qa.scene import (
    MAX_VALUES_PER_OBSERVABLE,
    MISSING_LIFETIME,
    SCENE_VIEW_END,
    SCENE_VIEW_START_PREFIX,
    SCENE_VIEW_START_SUFFIX,
    SceneMemory,
)


def state(
    scene="Lobby",
    observables=None,
    interactables=None,
    actions=None,
    screen=None,
    visuals=None,
) -> GameState:
    return GameState(
        scene=scene,
        screen=screen,
        interactables=interactables or [],
        visuals=visuals or [],
        observables=observables or {},
        recentActions=actions or [],
    )


def test_only_changes_are_recorded() -> None:
    memory = SceneMemory()
    memory.apply(state(observables={"Score.value": {"value": 0, "type": "int"}}))
    memory.apply(state(observables={"Score.value": {"value": 0, "type": "int"}}))
    memory.apply(state(observables={"Score.value": {"value": 100, "type": "int"}}))

    track = memory.observables["Score.value"]
    assert [point.value for point in track.values] == [0, 100]
    assert track.current == 100
    assert track.changes == 1


def test_path_between_observations_is_kept() -> None:
    """The point of a list: a value that moved twice while unobserved."""
    memory = SceneMemory()
    memory.apply(state(observables={"Hp": {"value": 100}}))
    watermark = memory.updates

    memory.apply(state(observables={"Hp": {"value": 80}}))
    memory.apply(state(observables={"Hp": {"value": 60}}))

    track = memory.observables["Hp"]
    assert track.values_since(watermark) == [80, 60]
    assert track.changed_since(watermark)


def test_scene_change_resets_everything() -> None:
    memory = SceneMemory()
    memory.apply(state(observables={"Lobby.thing": {"value": 1}}))
    memory.apply(state(scene="Battle", observables={"Battle.thing": {"value": 2}}))

    assert memory.scene == "Battle"
    assert "Lobby.thing" not in memory.observables
    assert memory.updates == 1


def test_absent_key_is_marked_gone_not_deleted() -> None:
    memory = SceneMemory()
    memory.apply(state(observables={"Dialog.content": {"value": "hi"}}))
    memory.apply(state(observables={}))

    assert memory.missing == ["Dialog.content"]
    assert memory.observables["Dialog.content"].current == "hi"


def test_missing_key_expires_after_its_lifetime() -> None:
    """A game that swaps UI without changing scene name must not accumulate remains."""
    memory = SceneMemory()
    memory.apply(state(observables={"Dialog.content": {"value": "hi"}}))
    for _ in range(MISSING_LIFETIME + 1):
        memory.apply(state(observables={}))

    assert "Dialog.content" not in memory.observables
    assert memory.missing == []


def test_interactables_are_replaced_not_merged() -> None:
    memory = SceneMemory()
    memory.apply(state(interactables=[Interactable(id=1, name="Old", type="button")]))
    memory.apply(state(interactables=[Interactable(id=2, name="New", type="button")]))

    assert [item.name for item in memory.interactables] == ["New"]


def test_history_is_bounded_and_says_so() -> None:
    memory = SceneMemory()
    for value in range(MAX_VALUES_PER_OBSERVABLE + 5):
        memory.apply(state(observables={"Counter": {"value": value}}))

    track = memory.observables["Counter"]
    assert len(track.values) == MAX_VALUES_PER_OBSERVABLE
    assert track.trimmed
    assert track.current == MAX_VALUES_PER_OBSERVABLE + 4


def test_actions_are_accumulated_without_duplicates() -> None:
    ran = ActionRecord(target="StartButton", name="onClick", success=True, at="t1")
    memory = SceneMemory()
    memory.apply(state(actions=[ran]))
    watermark = memory.updates
    # The same record arrives again in the next frame; the scene reports a window.
    memory.apply(state(actions=[ran]))

    assert len(memory.actions) == 1
    assert memory.actions_since(watermark) == []


def test_render_shows_change_and_failure() -> None:
    memory = SceneMemory()
    memory.apply(
        state(
            observables={"Score.value": {"value": 0}, "Title.content": {"value": "Lobby"}},
            interactables=[Interactable(id=1, name="Start", type="button", label="시작")],
        )
    )
    watermark = memory.updates
    # A frame is a full scene scan, so it carries the interactables again.
    memory.apply(
        state(
            observables={"Score.value": {"value": 100}, "Title.content": {"value": "Lobby"}},
            interactables=[Interactable(id=1, name="Start", type="button", label="시작")],
            actions=[
                ActionRecord(
                    target="Shop", name="buy", success=False, error="골드 부족", at="t2"
                )
            ],
        )
    )

    view = memory.render(watermark)

    assert "Score.value: 0 → 100" in view
    assert "Shop.buy → FAILED (골드 부족)" in view
    assert "unchanged: 1" in view
    assert "[1] Start (button) — 시작" in view


def test_render_aims_at_the_centre_of_an_element() -> None:
    """The pointer tools take a point, and the corner the game reports is not it.

    Doing the arithmetic here rather than in the prompt is the whole point: the
    numbers on this line go into `move_pointer` as they stand.
    """
    memory = SceneMemory()
    memory.apply(
        state(
            screen=Screen(w=1920, h=1080),
            interactables=[
                Interactable(
                    id=12,
                    name="Start",
                    type="button",
                    rect=Rect(x=420, y=300, w=200, h=60),
                )
            ],
        )
    )

    view = memory.render(0)

    assert "screen: 1920x1080 pixels" in view
    assert "[12] Start (button) @ 520,330 200x60" in view


def test_render_names_an_off_screen_element_instead_of_placing_it() -> None:
    """Its rect is real but outside the screen; aiming there would hit nothing."""
    memory = SceneMemory()
    memory.apply(
        state(
            interactables=[
                Interactable(
                    id=3,
                    name="NextPage",
                    type="button",
                    label="다음",
                    rect=Rect(x=2100, y=300, w=200, h=60),
                    onScreen=False,
                )
            ],
        )
    )

    view = memory.render(0)

    assert "[3] NextPage (button) (off screen) — 다음" in view
    assert "2200" not in view


def test_render_places_a_visual_the_same_way_as_an_interactable() -> None:
    """It is not on the actionable list, but the pointer tools reach it all the same."""
    memory = SceneMemory()
    memory.apply(
        state(
            interactables=[
                Interactable(
                    id=12,
                    name="Start",
                    type="button",
                    rect=Rect(x=420, y=300, w=200, h=60),
                )
            ],
            visuals=[
                Visual(
                    id=44,
                    name="enemy_goblin",
                    type="sprite",
                    sprite="goblin_idle",
                    rect=Rect(x=252, y=372, w=96, h=96),
                )
            ],
        )
    )

    view = memory.render(0)

    assert "on screen:" in view
    assert "[44] enemy_goblin (sprite) @ 300,420 96x96 — goblin_idle" in view
    # After the actionable list, so the section it belongs to is unambiguous.
    assert view.index("you can act on:") < view.index("on screen:")


def test_render_names_an_off_screen_visual_instead_of_placing_it() -> None:
    """Same rule as an interactable: a rect outside the screen is not a target."""
    memory = SceneMemory()
    memory.apply(
        state(
            visuals=[
                Visual(
                    id=7,
                    name="Backdrop",
                    type="image",
                    rect=Rect(x=2400, y=100, w=200, h=200),
                    onScreen=False,
                )
            ],
        )
    )

    view = memory.render(0)

    assert "[7] Backdrop (image) (off screen)" in view
    assert "2500" not in view


def test_render_omits_the_visuals_section_when_the_scene_sends_none() -> None:
    """An Orchestration server older than the visuals relay sends no such field."""
    memory = SceneMemory()
    memory.apply(state(interactables=[Interactable(id=1, name="Start", type="button")]))

    view = memory.render(0)

    assert "on screen:" not in view
    # The last content line, i.e. right before the end-of-view marker.
    content_lines = view.splitlines()
    assert content_lines[-2] == "  [1] Start (button)"


def test_render_wraps_the_view_in_start_and_end_markers() -> None:
    """`fold_stale_scenes` (app/agents/qa/context.py) locates a view by these,
    rather than guessing where `scene: ...` text starts or ends."""
    memory = SceneMemory()
    memory.apply(state(interactables=[Interactable(id=1, name="Start", type="button")]))
    memory.apply(state(interactables=[Interactable(id=1, name="Start", type="button")]))

    view = memory.render(0)

    start = f"{SCENE_VIEW_START_PREFIX}{memory.updates}{SCENE_VIEW_START_SUFFIX}"
    assert view.startswith(start)
    assert view.endswith(SCENE_VIEW_END)
    # The observation number in the marker matches the one in the view body.
    assert f"(observation {memory.updates})" in view


def test_render_says_nothing_extra_when_the_scene_carries_no_coordinates() -> None:
    """An Orchestration server older than the coordinate relay sends neither."""
    memory = SceneMemory()
    memory.apply(state(interactables=[Interactable(id=1, name="Start", type="button")]))

    view = memory.render(0)

    assert "[1] Start (button)" in view
    assert "@" not in view
    assert "screen:" not in view


def test_screen_size_survives_a_frame_that_omits_it() -> None:
    """It belongs to the window, not the frame, so silence is not a change."""
    memory = SceneMemory()
    memory.apply(state(screen=Screen(w=1920, h=1080)))
    memory.apply(state(scene="Battle"))

    assert memory.screen == Screen(w=1920, h=1080)
