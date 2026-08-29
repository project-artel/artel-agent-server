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
from app.qa.scene_context import SceneContext
from app.qa.screen import ScreenMap

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
    # 이 빌드의 씬별 capability 와 앵커 지식, 시나리오 시작에 한 번 받아 통째로 (ARTEL-612).
    #
    # 여기서 아무것도 조회하지 않는다. 부르는 것은 `app/qa/service.py` 이고 이 필드는 그
    # 결과를 담기만 한다 — 조회를 여기 두면 프레임만 아는 클래스가 네트워크 의존을 갖는다.
    # 없으면 `None` 이고, 그때 이 클래스는 종전과 한 글자도 다르지 않게 그린다.
    scene_context: SceneContext | None = None
    # 지도가 마지막으로 말한 `screen` (ARTEL-657). 여기서 아무것도 조회하지 않는다 —
    # 채우는 것은 `SCREEN_SETTLED` 와 `SCREEN_SELECTOR_PROPOSAL` 을 받는 `QaRunChannel`
    # 이다(ARTEL-668).
    #
    # 씬이 바뀌어도 안 비운다. 판정은 자기 `scene` 이름을 들고 있고 `render` 가 그 이름을
    # 맞대 보므로, 다른 `scene` 에서는 그리지 않으면서 되돌아왔을 때는 살아 있다. 여기서
    # 비우면 `scene` 을 한 번 나갔다 온 것만으로 판정이 영영 사라진다 — 저쪽은 화면이
    # 바뀔 때만 알리므로, 되돌아온 것이 화면 변화가 아니면 다음 통보가 안 온다.
    screen_map: ScreenMap = Field(default_factory=ScreenMap)
    # 맥락 블록을 마지막으로 그린 씬 이름. 씬이 바뀐 뒤 첫 렌더에만 그리게 하는 장부다.
    #
    # 블록은 씬이 바뀔 때만 바뀌는데 도구 결과는 대화에 쌓이므로, 매번 그리면 한 씬에
    # 머문 턴 수만큼 같은 문단이 컨텍스트에 남는다. 종전 라이브 뷰는 매 호출 교체되는
    # 꼬리라 이 값이 없어도 됐다 — 그 꼬리가 프롬프트 접두를 깨뜨려 없어졌다(ARTEL-621).
    #
    # 블록이 `None` 으로 나와도 이 값을 옮긴다. 조회에 없는 씬을 매 턴 다시 물어보는 것은
    # 답이 바뀌지 않는 질문을 반복하는 것이다.
    scene_context_drawn_for: str | None = None

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

    def current_scene(self) -> str:
        """지금 쥔 것 전부. `render` 가 주는 창 뷰와 달리 청했을 때만 나간다(ARTEL-673)."""
        if self.scene is None:
            page = self.pulse.current_scene()
            if page is None:
                return "No scene has been received yet."
            return self._with_scene_context(page)

        # GAME_STATE 갈래에서는 창의 시작이 곧 전량이다.
        return self.render(0)

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
            if pulse is None:
                return "No scene has been received yet."
            verdict = self.screen_map_block()
            # 판정이 `pulse` 뷰 **위**다. 아래는 지금 무엇이 움직였나이고, 이것은 그 움직임이
            # 지도에서 어느 화면으로 앉았나라 먼저 읽혀야 한다.
            #
            # 이 갈래를 빠뜨리면 실전에서 블록이 한 번도 안 뜬다. 실측 런은 `GAME_STATE` 가
            # 0장이고 `PULSE` 만 14489장이라, 화면을 그리는 자리가 여기뿐이다.
            body = pulse if verdict is None else f"{verdict}\n\n{pulse}"
            return self._with_scene_context(body)

        lines = [f"scene: {self.scene}  (observation {self.updates})"]
        if self.screen is not None:
            lines.append(f"screen: {self.screen.w}x{self.screen.h} pixels")
        verdict = self.screen_map_block()
        if verdict is not None:
            lines.append(verdict)

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
        # 맥락 블록은 마커 **밖**이다. `fold_stale_scenes` 가 이 마커 쌍을 통째로 자리표로
        # 바꾸는데, 그 안에 넣으면 씬 뷰 하나만 남기는 `fold` 에 블록도 함께 사라진다.
        # `fold` 되는 것은 화면이고, 블록은 그 화면이 무엇인지에 대한 설명이라 같이 갈
        # 이유가 없다.
        return self._with_scene_context(f"{start}\n{body}\n{SCENE_VIEW_END}")

    def screen_map_block(self) -> str | None:
        """지도가 지금 이 `scene` 에 대해 하는 말, 없으면 `None`.

        `scene` 이름을 `pulse` 에서도 읽는다. `pulse` 만 오는 게임에서는 `self.scene` 이
        끝까지 `None` 이고, 그것만 보면 블록이 영영 안 뜬다 — 맥락 블록이 같은 이유로 같은
        폴백을 쓴다.
        """
        return self.screen_map.render(self.scene or self.pulse.scene)

    def scene_context_block(self) -> str | None:
        """이 씬에 대해 이미 알려진 것. 조회가 그 씬을 싣지 않았으면 `None`.

        장부(`scene_context_drawn_for`)를 보지 않는다. 이미 그렸든 아니든 지금 씬의 블록을
        내놓는 자리이고, 압축이 이것을 쓴다 — 원장이 화면을 다시 말할 때 그 화면에 대한
        설명도 함께 가야, 블록을 실었던 도구 결과가 요약으로 대체돼도 남는다(ARTEL-622 가
        화면에 대해 세운 것과 같은 이유다).
        """
        if self.scene_context is None:
            return None
        return self.scene_context.render(self.scene or self.pulse.scene)

    def _with_scene_context(self, view: str) -> str:
        """`view` 아래에, 런이 이 씬으로 막 옮겨 왔을 때만 맥락 블록을 붙인다.

        아래인 것은 위가 게임이 **지금** 하고 있는 것이고 이것은 그것을 어디서 하고 있는지에
        대한 문서이기 때문이다. 종전에는 매 모델 호출 뒤에 붙던 라이브 뷰 안에 들어갔는데,
        그 뷰가 없어졌다(ARTEL-621) — 화면을 내는 자리가 도구 결과 하나뿐이므로 블록도
        그리로 온다. 도구 결과는 대화에 남으므로, 한 번 그리면 그 씬에 머무는 동안 그
        자리에 있다.

        씬 이름은 `pulse` 에서도 읽는다. `pulse` 만 오는 게임에서는 `self.scene` 이 끝까지
        `None` 이고, 그것만 보면 블록이 영영 안 뜬다.
        """
        if self.scene_context is None:
            return view
        name = self.scene or self.pulse.scene
        if not name or name == self.scene_context_drawn_for:
            return view
        self.scene_context_drawn_for = name
        block = self.scene_context_block()
        return view if block is None else f"{view}\n\n{block}"
