"""What the Agent remembers about the game, across observations.

A single GAME_STATE frame says what the screen is *now*. Almost every step's
`expected` is a statement about change — a score rises, a dialog closes — so a
snapshot alone carries no evidence for a verdict. This module keeps the frames
merged so the Agent can ask what changed since it last looked.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.qa.envelope import ActionRecord, GameState, Interactable

# Per key. Long enough to show a path like 100 → 80 → 60, short enough that a
# chatty value cannot grow the session without bound.
MAX_VALUES_PER_OBSERVABLE = 10

# Across the scene. Bounds what one observation can dump into the prompt.
MAX_ACTIONS = 40

# How many observations a key survives after it stops appearing.
#
# Kept for a while because something disappearing is evidence — a dialog that
# closed is often the very thing a step checks. Dropped eventually because a game
# that swaps its UI without changing the scene name would otherwise accumulate the
# remains of every screen it has ever shown.
MISSING_LIFETIME = 5


class Observation(BaseModel):
    """A value, and the observation it became that value on."""

    at: int
    value: Any


class ObservableTrack(BaseModel):
    """One observable's values over time — changes only, oldest first.

    Everything else (current, previous, how many times it changed) is derived
    rather than stored: storing them too would be the same fact in several
    places, and a missed update would make them disagree.
    """

    type: str | None = None
    values: list[Observation] = Field(default_factory=list)
    # Set when the bound dropped older values, so a reader is not told a wrong
    # change count.
    trimmed: bool = False

    @property
    def current(self) -> Any:
        return self.values[-1].value if self.values else None

    @property
    def changes(self) -> int:
        return max(len(self.values) - 1, 0)

    def changed_since(self, watermark: int) -> bool:
        return bool(self.values) and self.values[-1].at > watermark

    def values_since(self, watermark: int) -> list[Any]:
        return [point.value for point in self.values if point.at > watermark]

    def record(self, value: Any, at: int, type_: str | None) -> None:
        if type_ is not None:
            self.type = type_
        if self.values and self.values[-1].value == value:
            return
        self.values.append(Observation(at=at, value=value))
        if len(self.values) > MAX_VALUES_PER_OBSERVABLE:
            del self.values[:-MAX_VALUES_PER_OBSERVABLE]
            self.trimmed = True


def _unwrap(raw: Any) -> tuple[Any, str | None]:
    """Orchestration sends `{value, type}`; older payloads send a bare value."""
    if isinstance(raw, dict) and "value" in raw:
        type_ = raw.get("type")
        return raw["value"], type_ if isinstance(type_, str) else None
    return raw, None


class SceneMemory(BaseModel):
    """Frames merged into one picture of the current scene.

    Reset on scene change: observable keys are derived from node and component
    names, so a new scene is a new key space. Carrying the old keys over would
    let the Agent reason about elements that are no longer on screen.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    scene: str | None = None
    updates: int = 0
    interactables: list[Interactable] = Field(default_factory=list)
    observables: dict[str, ObservableTrack] = Field(default_factory=dict)
    # Keys seen earlier in this scene but absent from the latest frame, and the
    # observation each was last present on. Kept rather than deleted: something
    # disappearing is itself evidence.
    missing: list[str] = Field(default_factory=list)
    last_seen: dict[str, int] = Field(default_factory=dict)
    actions: list[ActionRecord] = Field(default_factory=list)
    actions_at: list[int] = Field(default_factory=list)

    def apply(self, state: GameState) -> None:
        if state.scene != self.scene:
            self.scene = state.scene
            self.updates = 0
            self.observables = {}
            self.missing = []
            self.last_seen = {}
            self.actions = []
            self.actions_at = []

        self.updates += 1
        at = self.updates

        # Replaced, not merged: ids can change between frames, and acting on a
        # stale id would target whatever now holds it.
        self.interactables = list(state.interactables)

        for key, raw in state.observables.items():
            value, type_ = _unwrap(raw)
            track = self.observables.get(key)
            if track is None:
                track = ObservableTrack()
                self.observables[key] = track
            track.record(value, at, type_)
            self.last_seen[key] = at

        seen = set(state.observables)
        expired = [
            key
            for key in self.observables
            if key not in seen and at - self.last_seen.get(key, at) > MISSING_LIFETIME
        ]
        for key in expired:
            del self.observables[key]
            self.last_seen.pop(key, None)
        self.missing = [key for key in self.observables if key not in seen]

        for record in state.recentActions:
            if record in self.actions:
                continue
            self.actions.append(record)
            self.actions_at.append(at)
        if len(self.actions) > MAX_ACTIONS:
            del self.actions[:-MAX_ACTIONS]
            del self.actions_at[:-MAX_ACTIONS]

    def actions_since(self, watermark: int) -> list[ActionRecord]:
        return [
            record
            for record, at in zip(self.actions, self.actions_at)
            if at > watermark
        ]

    def render(self, watermark: int) -> str:
        """The Agent-facing view: what changed since it last looked.

        Deliberately not a dump of the frame. The unchanged majority is summarised
        so the few things that moved are readable.
        """
        if self.scene is None:
            return "No scene has been received yet."

        lines = [f"scene: {self.scene}  (observation {self.updates})"]

        changed = {
            key: track
            for key, track in self.observables.items()
            if track.changed_since(watermark)
        }
        if changed:
            lines.append("")
            lines.append("changed since your last look:")
            for key, track in changed.items():
                path = track.values_since(watermark)
                # The value it held when last seen, so the change reads as a move
                # from something rather than appearing from nowhere.
                before = [p.value for p in track.values if p.at <= watermark]
                arrow = " → ".join(repr(v) for v in ([before[-1]] if before else []) + path)
                note = "  (earlier changes trimmed)" if track.trimmed else ""
                lines.append(f"  {key}: {arrow}{note}")
        else:
            lines.append("")
            lines.append("changed since your last look: nothing")

        if self.missing:
            lines.append(f"gone from the scene: {', '.join(self.missing)}")

        unchanged = len(self.observables) - len(changed)
        if unchanged > 0:
            lines.append(f"unchanged: {unchanged}")
            for key, track in self.observables.items():
                if key not in changed:
                    lines.append(f"  {key} = {track.current!r}")

        ran = self.actions_since(watermark)
        if ran:
            lines.append("")
            lines.append("the game ran since your last look:")
            for record in ran:
                outcome = "ok" if record.success else f"FAILED ({record.error})"
                lines.append(f"  {record.target}.{record.name} → {outcome}")

        lines.append("")
        lines.append("you can act on:")
        for item in self.interactables:
            label = item.label or item.placeholder or ""
            suffix = f" — {label}" if label else ""
            lines.append(f"  [{item.id}] {item.name} ({item.type}){suffix}")

        return "\n".join(lines)
