from app.qa.envelope import ActionRecord, GameState, Interactable
from app.qa.scene import MAX_VALUES_PER_OBSERVABLE, MISSING_LIFETIME, SceneMemory


def state(scene="Lobby", observables=None, interactables=None, actions=None) -> GameState:
    return GameState(
        scene=scene,
        interactables=interactables or [],
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
