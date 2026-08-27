"""What the Agent remembers about the game, across observations.

A single GAME_STATE frame says what the screen is *now*. Almost every step's
`expected` is a statement about change — a score rises, a dialog closes — so a
snapshot alone carries no evidence for a verdict. This module keeps the frames
merged so the Agent can ask what changed since it last looked.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.qa.envelope import ActionRecord, GameState, Interactable, Rect, Screen, Visual
from app.qa.pulse import PulseMemory

# Per key. Long enough to show a path like 100 → 80 → 60, short enough that a
# chatty value cannot grow the session without bound.
MAX_VALUES_PER_OBSERVABLE = 10

# Across the scene. Bounds what one observation can dump into the prompt.
MAX_ACTIONS = 40

# How many of those the live view carries. Smaller than `MAX_ACTIONS` because
# this one is rewritten on EVERY model call, not once per observation: forty
# lines a turn would cost more than the rest of the view put together. Ten is
# enough to cover the step being judged, which is all this view is for — the
# tool-result view still reports every action since the agent last looked.
MAX_ACTIONS_IN_LIVE_VIEW = 10

# How many observations a key survives after it stops appearing.
#
# Kept for a while because something disappearing is evidence — a dialog that
# closed is often the very thing a step checks. Dropped eventually because a game
# that swaps its UI without changing the scene name would otherwise accumulate the
# remains of every screen it has ever shown.
MISSING_LIFETIME = 5

# `render`'s output is wrapped in these so a later pass — `fold_stale_scenes` in
# `app/agents/qa/context.py` — can find exactly where the view starts and ends
# inside a tool message that may also carry action-outcome lines above it and an
# operator block below it. A marker beats guessing at `scene: ` text: nothing
# stops a game's own scene name, or an observable's value, from containing that
# word, and a wrong guess would clip a view rather than fold it whole. The
# observation number rides in the start marker so a reader of a folded message
# does not have to parse the view itself to say which one went missing.
SCENE_VIEW_START_PREFIX = "<<scene view "
SCENE_VIEW_START_SUFFIX = ">>"
SCENE_VIEW_END = "<<end scene view>>"


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


def _where(item: Interactable | Visual) -> str:
    """Where to aim at an element, as `@ centreX,centreY widthxheight`.

    The centre rather than the reported corner, because the centre is what the
    pointer tools take and the Agent should not be left doing arithmetic on the
    way to a click. Empty when the scene reports no rect, which is what an
    Orchestration server older than the coordinate relay sends.
    """
    # An off-screen element still has a rect, somewhere outside the screen.
    # Naming it beats offering coordinates that would land on nothing.
    if not item.onScreen:
        return " (off screen)"
    if item.rect is None:
        return ""
    x, y = item.rect.center
    return f" @ {x},{y} {item.rect.w}x{item.rect.h}"


def _interactable_line(item: Interactable) -> str:
    label = item.label or item.placeholder or ""
    suffix = f" — {label}" if label else ""
    return f"  [{item.id}] {item.name} ({item.type}){_where(item)}{suffix}"


def _visual_line(visual: Visual) -> str:
    # The sprite asset, when there is one — a name like `goblin_hurt` says what
    # the element currently shows, which the node's own name does not.
    suffix = f" — {visual.sprite}" if visual.sprite else ""
    return f"  [{visual.id}] {visual.name} ({visual.type}){_where(visual)}{suffix}"


def _action_line(record: ActionRecord, at: int | None = None) -> str:
    outcome = "ok" if record.success else f"FAILED ({record.error})"
    # The observation only in the live view, where there is no "since your last
    # look" to place the action against.
    when = f"   [obs {at}]" if at is not None else ""
    return f"  {record.target}.{record.name} → {outcome}{when}"


def _visual_keys(visuals: list[Visual]) -> list[str]:
    """A stable key per visual, to hang its rect history on.

    Not the id: ids are reassigned between frames (see `apply`), so a track keyed
    by one would splice two different sprites' positions into a single path. The
    name is what survives. Names do repeat — a scene with five `Enemy` sprites —
    so duplicates within a frame are numbered by the order the scene lists them
    in, which is its traversal order and stable between frames.
    """
    seen: dict[str, int] = {}
    keys: list[str] = []
    for visual in visuals:
        count = seen.get(visual.name, 0) + 1
        seen[visual.name] = count
        keys.append(visual.name if count == 1 else f"{visual.name}#{count}")
    return keys


def _path(track: ObservableTrack, show) -> str:
    """A track's values as `a → b → c   [obs 1, 4, 9]`.

    The observation numbers ride along because the values alone do not say when
    a change happened, and "did this move before or after I clicked" is most of
    what a verdict turns on. Omitted for a value that never changed — there is
    no when to report.
    """
    if not track.values:
        return "(none)"
    values = " → ".join(show(point.value) for point in track.values)
    if len(track.values) == 1:
        return values
    return f"{values}   [obs {', '.join(str(point.at) for point in track.values)}]"


def _rect_path(track: ObservableTrack) -> str:
    rects = [point.value for point in track.values]
    # Repeating the size on every entry is noise when only the position moved,
    # which is what a sprite usually does.
    same_size = len({(rect.w, rect.h) for rect in rects}) == 1

    def show(rect: Rect) -> str:
        x, y = rect.center
        return f"{x},{y}" if same_size else f"{x},{y} {rect.w}x{rect.h}"

    return _path(track, show)


class SceneMemory(BaseModel):
    """Frames merged into one picture of the current scene.

    Reset on scene change: observable keys are derived from node and component
    names, so a new scene is a new key space. Carrying the old keys over would
    let the Agent reason about elements that are no longer on screen.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    scene: str | None = None
    # Observations in the CURRENT scene. Reset with the scene, because every
    # observable's timeline is relative to it.
    updates: int = 0
    # Frames applied over the whole run, never reset. `updates` cannot answer
    # "did a new frame arrive", since a scene change sends it back to 1 and a
    # caller comparing it to what it saw before would read a transition as
    # silence — the one moment it most needs to see.
    frames: int = 0
    interactables: list[Interactable] = Field(default_factory=list)
    visuals: list[Visual] = Field(default_factory=list)
    # None until a frame carries it; an older Orchestration server sends none.
    screen: Screen | None = None
    observables: dict[str, ObservableTrack] = Field(default_factory=dict)
    # Keys seen earlier in this scene but absent from the latest frame, and the
    # observation each was last present on. Kept rather than deleted: something
    # disappearing is itself evidence.
    missing: list[str] = Field(default_factory=list)
    last_seen: dict[str, int] = Field(default_factory=dict)
    # One rect path per visual, keyed by `_visual_keys`. The same track type an
    # observable uses, because a sprite's position IS an observable value that
    # changes over observations — same dedup, same 10-entry bound, same reason.
    # `interactables` are deliberately not tracked: their rects are read to aim a
    # click at, and a button's history has never decided a verdict.
    visual_rects: dict[str, ObservableTrack] = Field(default_factory=dict)
    visual_last_seen: dict[str, int] = Field(default_factory=dict)
    actions: list[ActionRecord] = Field(default_factory=list)
    actions_at: list[int] = Field(default_factory=list)
    # 판독 채널(ARTEL-401). 씬이 바뀌어도 비우지 않는다 — 판독 자신이 씬 전환에서 전량을
    # 보내며 스스로 갈아치우고, 여기서 또 비우면 그 전량이 도착하기 전 창이 빈 채로 남는다.
    #
    # 위의 `observables` 와 함께 산다. 지금은 두 출처가 공존하고 판독이 우선인데, 우선한다는
    # 것은 값을 덮어쓴다는 뜻이 아니라 **자기 블록으로 따로 실린다**는 뜻이다: 스냅샷에서
    # 복원한 값과 게임에서 직접 읽은 값이 어긋날 때 어느 쪽이 무엇인지 읽는 쪽이 가릴 수
    # 있어야 한다. `states` 를 걷어내 하나로 만드는 것은 ARTEL-400 이다.
    pulse: PulseMemory = Field(default_factory=PulseMemory)

    def apply(self, state: GameState) -> None:
        if state.scene != self.scene:
            self.scene = state.scene
            self.updates = 0
            self.observables = {}
            self.missing = []
            self.last_seen = {}
            self.visual_rects = {}
            self.visual_last_seen = {}
            self.actions = []
            self.actions_at = []

        self.updates += 1
        self.frames += 1
        at = self.updates

        # Replaced, not merged: ids can change between frames, and acting on a
        # stale id would target whatever now holds it. The same holds of a
        # visual's rect — a kept one would aim at where the sprite used to be.
        self.interactables = list(state.interactables)
        self.visuals = list(state.visuals)

        # The rect is replaced with the frame, but its path is not: a sprite that
        # crossed the screen while the agent was not looking is exactly the kind
        # of change a step's `expected` is about, and a snapshot cannot show it.
        for key, visual in zip(_visual_keys(self.visuals), self.visuals):
            if visual.rect is None:
                continue
            track = self.visual_rects.get(key)
            if track is None:
                track = ObservableTrack()
                self.visual_rects[key] = track
            track.record(visual.rect, at, "rect")
            self.visual_last_seen[key] = at
        # Same lifetime as a missing observable, for the same reason: a sprite
        # that left the screen is evidence for a while, and litter after that.
        for key in [
            key
            for key, last in self.visual_last_seen.items()
            if at - last > MISSING_LIFETIME
        ]:
            del self.visual_rects[key]
            del self.visual_last_seen[key]

        # Kept across a frame that omits it — the screen belongs to the window,
        # not to the frame, so silence is "not reported" rather than "gone". A
        # resize still lands, since a reported size overwrites.
        if state.screen is not None:
            self.screen = state.screen

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

    def render(self, watermark: int, since_action: int | None = None) -> str:
        """The Agent-facing view: what changed since it last looked.

        Deliberately not a dump of the frame. The unchanged majority is summarised
        so the few things that moved are readable.

        `since_action` 은 이 행위가 끝난 Unity 프레임이다. 판독이 유일한 출처인 지금, 그보다
        뒤에 잡힌 판독만이 이 행위의 결과다 — 그것이 없으면 창의 경계가 타이머가 되고, 액션
        직후 도착한 배치가 액션 **전**에 잡힌 것일 수 있다(ARTEL-621).
        """
        if self.scene is None:
            pulse = self.pulse.since_action(since_action)
            # 판독만 도착한 경우. GAME_STATE 가 없다고 판독을 감추면 그 채널이 유일한
            # 상태 출처가 되는 날(ARTEL-400) 화면이 통째로 비어 보인다.
            #
            # 마커로 감싸지 않는다. 접기(`fold_stale_scenes`)가 그 마커를 찾아 자리표로
            # 바꾸는데, 이 갈래가 내는 것은 **행위의 기록**이라 접히면 안 된다. 씬 페이지가
            # 다음 도구 결과 하나에 먹히고(`DEFAULT_KEEP_SCENES = 1` 이 목록 전체 기준이다),
            # 옛 메시지를 고쳐 쓰는 일이라 프롬프트 접두까지 깨진다.
            return pulse if pulse is not None else "No scene has been received yet."

        lines = [f"scene: {self.scene}  (observation {self.updates})"]
        if self.screen is not None:
            lines.append(f"screen: {self.screen.w}x{self.screen.h} pixels")

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
            lines.extend(_action_line(record) for record in ran)

        lines.append("")
        lines.append("you can act on:")
        lines.extend(_interactable_line(item) for item in self.interactables)

        # Its own section, after the actionable list, because these are reachable
        # by a different route: a point rather than an id. Omitted entirely when
        # the scene reports none, so an older Orchestration server renders as before.
        if self.visuals:
            lines.append("")
            lines.append("on screen:")
            lines.extend(_visual_line(visual) for visual in self.visuals)

        pulse = self.pulse.render()
        if pulse is not None:
            lines.append("")
            lines.append(pulse)

        body = "\n".join(lines)
        start = f"{SCENE_VIEW_START_PREFIX}{self.updates}{SCENE_VIEW_START_SUFFIX}"
        return f"{start}\n{body}\n{SCENE_VIEW_END}"
