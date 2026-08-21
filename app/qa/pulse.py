"""What the Agent remembers from the pulse channel.

`GAME_STATE` says what the screen is *now*, and `app/qa/scene.py` reconstructs
change by diffing one snapshot against the last. Pulse hands that over as the
original: the SDK reads watched members every 0.1s and reports the ones that
moved, so "the score rose" arrives as a fact rather than as an inference drawn
between two observations.

Two kinds of reading arrive on the same channel and `whole` tells them apart:

    whole=true    everything the reading can see. Sent on the first reading and
                  whenever the scene changes, and after a delivery is lost
    whole=false   only what moved since the last one

A delta lands on top of what is already held. **An object the reading says
nothing about keeps what it had, including which list it was in** — its
absence is not news, and had it changed the reading would have carried it.

Values are kept exactly as they arrive. What `flag == 1` means is a question
for whoever writes the spec, not for this module: interpreting it here would
put a second opinion between the game and the step that judges it.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# How many readings a key survives after it stops being reported.
#
# Only used for the change counter below — held objects and their values are
# never dropped, because a value that stops moving is still the value.
MAX_TRACKED_KEYS = 4096

# 판독을 도착한 순서대로 몇 개나 들고 있는지.
#
# `scene.py` 의 `MAX_VALUES_PER_OBSERVABLE` 과 같은 값이고 같은 이유다: 접힌 상태는 지금 무엇이
# 참인지만 말하고, 어떻게 거기 이르렀는지는 말하지 않는다. 스텝의 `expected` 는 거의가 변화에
# 대한 진술이라 순서가 근거의 절반이다.
#
# 문서 전체가 아니라 한 줄씩 남긴다. 전량 판독이 실측 약 18 KB 라 열 개를 통째로 실으면 그것만
# 으로 프롬프트가 채워지고, 순서를 읽는 데 필요한 것은 몇 번째 판독이 무엇을 움직였나뿐이다.
MAX_READING_LOG = 10

PULSE_VIEW_START = "<<pulse>>"
PULSE_VIEW_END = "<<end pulse>>"


class PulseMember(BaseModel):
    """One watched member on one object, as the reading reported it.

    `value` is deliberately `Any`. The channel carries scalars as well as the
    shapes the SDK uses for a reference (`{"path": ..., "world": ...}`), a
    sprite, an animator state, a label, or a bare count — and a reader that
    narrowed this to a scalar would drop the half that names what a step is
    pointing at.
    """

    model_config = ConfigDict(extra="allow")

    # The declaring type, as the analyser named it. Kept apart from `member`
    # because two objects can offer the same property name.
    on: str | None = None
    member: str | None = None
    # Which of several same-typed components on one object this is. The SDK
    # counts them because a GameObject may carry a behaviour twice, and without
    # it the second overwrites the first.
    among: int | None = None
    value: Any = None
    # False when the reading read it because it could, rather than because the
    # evidence named it. The distinction is the whole point of carrying it: a
    # condition written against a named member means more than one that happens
    # to be readable.
    asked: bool | None = None

    @property
    def key(self) -> str:
        return f"{self.on or ''}::{self.member or ''}#{self.among or 0}"


class PulseStatic(BaseModel):
    """A value that hangs off no GameObject.

    Kept in its own table rather than under an object, because it belongs to
    none — folding statics in beside instance values would invent an owner.
    """

    model_config = ConfigDict(extra="allow")

    declaring: str | None = None
    member: str | None = None
    type: str | None = None
    value: Any = None

    @property
    def key(self) -> str:
        return f"{self.declaring or ''}::{self.member or ''}"


class PulseObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene: str | None = None
    # The SDK's instance id. It does not survive the process, which is why
    # `selector` rides along and why the key below prefers it.
    id: int | None = None
    path: str | None = None
    selector: str | None = None
    world: dict[str, Any] | None = None
    offers: dict[str, Any] | None = None
    members: list[PulseMember] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.scene or ''}/{self.selector or self.path or ''}"


class PulseReading(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int | None = Field(default=None, alias="schema")
    reading: int | None = None
    frame: int | None = None
    scene: str | None = None
    whole: bool = False
    statics: list[PulseStatic] = Field(default_factory=list)
    active: list[PulseObject] = Field(default_factory=list)
    deactive: list[PulseObject] = Field(default_factory=list)
    # Written even when empty: an empty list and a missing field are different
    # claims, and the reading means the first.
    changed: list[str] = Field(default_factory=list)
    watching: int | None = None
    unresolved: int | None = None
    # What the reading could not read. Carried so a reader can tell "nothing
    # moved" from "nothing was legible".
    unwatchable: int | None = None
    gaps: list[str] = Field(default_factory=list)


class ReadingLog(BaseModel):
    """도착한 판독 하나에 대해 남기는 한 줄."""

    model_config = ConfigDict(extra="allow")

    reading: int | None = None
    frame: int | None = None
    whole: bool = False
    # 이 판독이 움직였다고 말한 키들. 값이 아니라 이름만 — 값은 접힌 상태가 들고 있고,
    # 여기서 또 들면 같은 사실이 어긋날 자리가 둘이 된다.
    changed: list[str] = Field(default_factory=list)


class _HeldObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene: str | None = None
    path: str | None = None
    selector: str | None = None
    world: dict[str, Any] | None = None
    offers: dict[str, Any] | None = None
    # True when the object last arrived under `active`.
    live: bool = True
    members: dict[str, PulseMember] = Field(default_factory=dict)


class PulseMemory(BaseModel):
    """Readings folded together, newest on top.

    Mirrors the merge the SDK's own reading viewer does (`tools/watch-readings.py`),
    which is the only other reader of this channel — two readers disagreeing about
    what a delta means would be worse than either rule being wrong.
    """

    model_config = ConfigDict(extra="allow")

    readings: int = 0
    wholes: int = 0
    scene: str | None = None
    reading: int | None = None
    frame: int | None = None
    watching: int | None = None
    unresolved: int | None = None
    unwatchable: int | None = None
    statics: dict[str, PulseStatic] = Field(default_factory=dict)
    held: dict[str, _HeldObject] = Field(default_factory=dict)
    # How many readings each key moved in. This is the "it moved several times"
    # signal: a step that expects one change reads differently when the value
    # bounced five times and landed back.
    moves: dict[str, int] = Field(default_factory=dict)
    # 도착한 순서대로, 오래된 것이 앞. 상한을 넘으면 앞에서 버린다(FIFO).
    log: list[ReadingLog] = Field(default_factory=list)
    # 상한에 걸려 버린 판독이 있었나. 읽는 쪽이 "이것이 전부"라고 잘못 읽지 않도록.
    trimmed: bool = False

    @property
    def seen(self) -> bool:
        return self.readings > 0

    def apply(self, reading: PulseReading) -> None:
        self.readings += 1

        # A whole reading replaces rather than adds. Keeping the old rows would
        # leave behind objects the game has since destroyed, and the reading
        # says outright that this is everything it can see.
        if reading.whole:
            self.wholes += 1
            self.held = {}
            self.statics = {}

        if reading.scene is not None:
            self.scene = reading.scene
        for field in ("reading", "frame", "watching", "unresolved", "unwatchable"):
            value = getattr(reading, field)
            if value is not None:
                setattr(self, field, value)

        for key in reading.changed:
            self.moves[key] = self.moves.get(key, 0) + 1
        if len(self.moves) > MAX_TRACKED_KEYS:
            # Oldest-first is not available here, so drop the quietest: a key
            # that moved once is the least informative thing to keep.
            for key in sorted(self.moves, key=self.moves.get)[: len(self.moves) - MAX_TRACKED_KEYS]:
                del self.moves[key]

        self.log.append(
            ReadingLog(
                reading=reading.reading,
                frame=reading.frame,
                whole=reading.whole,
                changed=list(reading.changed),
            )
        )
        if len(self.log) > MAX_READING_LOG:
            # 앞에서 버린다. 최신이 가장 쓸모 있고, 오래된 판독은 이미 접혀 상태에 들어갔다.
            del self.log[: len(self.log) - MAX_READING_LOG]
            self.trimmed = True

        for entry in reading.statics:
            self.statics[entry.key] = entry

        # Which list an object arrives in is what says whether it is on. It is
        # not a field on the object, so it cannot disagree with itself.
        for live, arrived in ((True, reading.active), (False, reading.deactive)):
            for obj in arrived:
                was = self.held.get(obj.key)
                held = _HeldObject(
                    scene=obj.scene or (was.scene if was else None),
                    path=obj.path or (was.path if was else None),
                    selector=obj.selector or (was.selector if was else None),
                    world=obj.world or (was.world if was else None),
                    offers=obj.offers or (was.offers if was else None),
                    live=live,
                    members=dict(was.members) if was else {},
                )
                for member in obj.members:
                    held.members[member.key] = member
                self.held[obj.key] = held

    def render(self) -> str | None:
        """The pulse view, or None when no reading has arrived.

        None is what keeps an SDK that sends no pulse reading exactly as it was:
        nothing is appended, so every prompt is byte-identical to before.
        """
        if not self.seen:
            return None

        lines = [PULSE_VIEW_START]
        head = f"reading {self.reading}"
        if self.frame is not None:
            head += f" · frame {self.frame}"
        if self.scene:
            head += f" · scene {self.scene}"
        lines.append(head)
        if self.unwatchable:
            # Said out loud so "nothing moved" is not read as "nothing is there".
            lines.append(f"could not read: {self.unwatchable}")

        if self.log:
            lines.append("")
            head = f"readings, oldest first (last {MAX_READING_LOG})"
            if self.trimmed:
                # 열 개가 전부라고 읽으면 그 앞을 없었던 일로 세게 된다.
                head += " — earlier readings dropped"
            lines.append(head + ":")
            for entry in self.log:
                mark = "whole" if entry.whole else "delta"
                moved = ", ".join(entry.changed) if entry.changed else "nothing moved"
                lines.append(f"  {entry.reading} ({mark}): {moved}")
            lines.append("")

        if self.statics:
            lines.append("statics:")
            for key in sorted(self.statics):
                entry = self.statics[key]
                name = f"{(entry.declaring or '').split('.')[-1]}.{entry.member}"
                lines.append(f"  {name} = {entry.value!r}{self._moved(key)}")

        objects = sorted(self.held.items(), key=lambda pair: (not pair[1].live, pair[0]))
        for key, obj in objects:
            if not obj.members:
                continue
            where = obj.selector or obj.path or key
            lines.append(f"{where}{'' if obj.live else ' (off)'}:")
            for member_key in sorted(obj.members):
                member = obj.members[member_key]
                name = f"{(member.on or '').split('.')[-1]}.{member.member}"
                asked = "" if member.asked is not False else " (unasked)"
                moved = self._moved(f"{key}|{member_key}", f"{where}|{member_key}")
                lines.append(f"  {name} = {member.value!r}{asked}{moved}")

        lines.append(PULSE_VIEW_END)
        return "\n".join(lines)

    def _moved(self, *keys: str) -> str:
        """How many readings this key moved in, when that is more than one.

        The reading names a moved key with its own spelling, which this cannot
        reconstruct exactly — so every candidate is tried and the first hit
        wins. A miss costs the annotation, not the value.
        """
        for key in keys:
            count = self.moves.get(key)
            if count and count > 1:
                return f"  [moved in {count} readings]"
        return ""
