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

from app.qa.envelope import center_of

# 한 번에 들여다볼 수 있는 객체 수. 부분 일치가 많이 걸려도 프롬프트를 삼키지 않도록.
MAX_INSPECTED = 5

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

# 한 판독의 로그 줄이 이름 대는 키의 상한.
#
# 전량 판독은 `changed` 에 **감시 중인 키를 전부** 담는다 — SDK 의 장부가 직전 값과 다른 것을
# 넣는데 전량 판독에는 직전이 없기 때문이다(`LiveState.Ledger.Say`). 샘플 게임 실측이
# `watching 111` 이라 그것을 한 줄에 다 쓰면 로그 한 줄이 화면을 덮고, 더 큰 게임에서는 더하다.
#
# 세는 것과 이름 대는 것을 가른다. 몇 개가 움직였나는 언제나 정확하고, 어느 것인지는 여기까지다.
MAX_CHANGED_NAMED = 8

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
    # 화면 좌표. 조준의 대체 수단이고, 화면 크기와 맞대면 화면 안인지도 나온다.
    rect: dict[str, Any] | None = None
    offers: dict[str, Any] | None = None
    members: list[PulseMember] = Field(default_factory=list)
    # 컴포넌트별로 묶인 멤버. `on` 을 멤버마다 되풀이하지 않으려고 SDK 가 이렇게 낸다
    # (ARTEL-540) — 한 문서에서 `on` 316개 중 295개가 같은 값이었다.
    by: list["PulseComponent"] = Field(default_factory=list)

    def flatten(self, scene: str | None) -> "PulseObject":
        """읽는 쪽이 쓰던 모양으로 되돌린다.

        접는 것은 전송의 사정이지 이 메모리의 사정이 아니다. 여기서 한 번 펴 두면 병합도
        렌더도 종전 그대로다 — 그 둘이 접힌 모양을 알 이유가 없다.
        """
        if not self.by and self.scene is not None:
            return self

        spread = list(self.members)
        for group in self.by:
            for member in group.m:
                spread.append(member.model_copy(update={"on": group.on}))

        # 최상위와 같은 씬은 객체가 제 이름을 대지 않는다. 다른 씬의 객체만 댄다.
        return self.model_copy(update={"members": spread, "by": [], "scene": self.scene or scene})

    @property
    def key(self) -> str:
        return f"{self.scene or ''}/{self.selector or self.path or ''}"


class PulseComponent(BaseModel):
    """한 컴포넌트가 내놓은 멤버들. `on` 을 한 번만 쓴다."""

    model_config = ConfigDict(extra="allow")

    on: str | None = None
    m: list[PulseMember] = Field(default_factory=list)


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
    #
    # `MAX_CHANGED_NAMED` 까지만 든다. 몇 개였는지는 [moved] 가 따로 들고 있으므로 잘라도
    # "몇 개가 움직였나" 는 잃지 않는다.
    changed: list[str] = Field(default_factory=list)
    # 이 판독이 움직였다고 말한 키의 총 개수. `changed` 가 잘렸는지는 이 값과 길이로 안다.
    moved: int = 0


def _key_line(offered: Any) -> str:
    """키 하나를 사람이 고를 수 있게 쓴다.

    이름만으로는 못 고른다. Map 씬의 QA 가 화살표 다섯과 Return 을 대등하게 받고 위/아래를
    눌러 보다 전투에 진입하지 못했다 — 근거는 `Return` 이 씬을 바꾼다는 것을 알고 있었는데
    그 앎이 채널에서 버려지고 있었다(ARTEL-539).

    옛 SDK 는 문자열만 보낸다. 그때는 종전대로 이름만 쓴다.
    """
    if not isinstance(offered, dict):
        return str(offered)

    name = offered.get("key") or "?"
    does = offered.get("does")
    if not does:
        # 빈 것과 없는 것을 같이 다룬다. 어느 쪽이든 "무엇을 하는지 모른다" 이고, 그것을
        # "아무 일도 안 한다" 로 읽히게 두지 않는다.
        return f"{name} (effect unknown)"
    return f"{name} → {'; '.join(str(d) for d in does)}"


class _HeldObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene: str | None = None
    # 액션이 대상을 지목하는 값. 접을 때 버리면 독자는 무엇이 바뀌었는지 알면서
    # 그것을 건드릴 방법이 없다 — 판독이 이것을 싣는 이유가 그것이다.
    id: int | None = None
    path: str | None = None
    selector: str | None = None
    world: dict[str, Any] | None = None
    rect: dict[str, Any] | None = None
    offers: dict[str, Any] | None = None
    # True when the object last arrived under `active`.
    live: bool = True
    members: dict[str, PulseMember] = Field(default_factory=dict)
    # 멤버마다 그 값이 마지막으로 도착한 판독 번호. 델타만 그리려면 "언제 온 것인가"가
    # 필요한데, 값 자체는 그것을 말하지 않는다 — 같은 값이 두 번 오면 구분이 안 된다.
    #
    # 판독 번호를 쓰는 이유: 판독은 이미 델타라 도착했다는 것 자체가 움직였다는 뜻이다.
    # 도구가 언제 화면을 봤는지와는 무관하게 셀 수 있다.
    at: dict[str, int] = Field(default_factory=dict)


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
    # statics 도 객체 멤버와 같은 이유로 도착 시점을 남긴다.
    static_at: dict[str, int] = Field(default_factory=dict)
    # 마지막으로 그린 판독 번호. 이 장부를 여기 두는 이유는 "무엇까지 보여줬나" 가 이 메모리
    # 자신의 사정이기 때문이다 — 부르는 쪽마다 따로 세면 둘이 어긋날 자리가 생긴다.
    drawn: int = 0

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
                changed=list(reading.changed[:MAX_CHANGED_NAMED]),
                moved=len(reading.changed),
            )
        )
        if len(self.log) > MAX_READING_LOG:
            # 앞에서 버린다. 최신이 가장 쓸모 있고, 오래된 판독은 이미 접혀 상태에 들어갔다.
            del self.log[: len(self.log) - MAX_READING_LOG]
            self.trimmed = True

        for entry in reading.statics:
            self.statics[entry.key] = entry
            self.static_at[entry.key] = self.readings

        # Which list an object arrives in is what says whether it is on. It is
        # not a field on the object, so it cannot disagree with itself.
        for live, arrived in ((True, reading.active), (False, reading.deactive)):
            for folded in arrived:
                obj = folded.flatten(reading.scene)
                was = self.held.get(obj.key)
                held = _HeldObject(
                    scene=obj.scene or (was.scene if was else None),
                    # 0 이 유효한 id 는 아니지만 None 과 가리려면 is not None 이어야 한다.
                    id=obj.id if obj.id is not None else (was.id if was else None),
                    path=obj.path or (was.path if was else None),
                    selector=obj.selector or (was.selector if was else None),
                    world=obj.world or (was.world if was else None),
                    rect=obj.rect or (was.rect if was else None),
                    offers=obj.offers or (was.offers if was else None),
                    live=live,
                    members=dict(was.members) if was else {},
                )
                held.at = dict(was.at) if was else {}
                for member in obj.members:
                    held.members[member.key] = member
                    held.at[member.key] = self.readings
                self.held[obj.key] = held

    def render(self, since: int | None = None) -> str | None:
        """The pulse view, or None when no reading has arrived.

        `since` 는 마지막으로 그린 판독 번호다. 그 뒤에 도착한 값만 그린다 — 판독은 이미
        델타라 도착했다는 것 자체가 움직였다는 뜻이고, 도착하지 않은 값은 독자가 지난번에
        이미 읽었다.

        **조작할 수 있는 것은 창과 무관하게 매번 그린다.** 그것이 없으면 에이전트가 무엇을
        누를 수 있는지 물어봐야 하고, 그 왕복이 이 채널이 없애려던 것이다. 대신 값은 싣지
        않는다 — 안 움직인 값은 이미 알고 있다.

        `since=0` 은 종전과 같은 전량이다. 처음 보는 독자에게는 전부가 새것이다.

        None is what keeps an SDK that sends no pulse reading exactly as it was:
        nothing is appended, so every prompt is byte-identical to before.
        """
        if not self.seen:
            return None

        # 부르는 쪽이 창을 정하지 않으면 지난번에 그린 자리부터다. 그리고 나면 그 자리를
        # 옮긴다 — 같은 값을 두 번 그리지 않기 위해서다.
        if since is None:
            since = self.drawn
        self.drawn = self.readings

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

        # 로그도 창을 탄다. 지난번에 그린 판독을 다시 적으면 그만큼이 매 턴 반복된다.
        #
        # 그리고 **한 종류만 계속 움직이는 판독은 한 줄로 접는다.** 실측에서 전투 중 변화의
        # 98%가 `SlimeAnimator::spriteRenderer` 하나였다 — 애니메이션이 스프라이트를 갈아
        # 끼우는 것이지 QA 가 판정할 값이 아닌데, 그것이 로그 열 줄을 독차지했다.
        window = [e for e in self.log if (e.reading or 0) > since] or self.log[-1:]
        if window:
            lines.append("")
            head = f"readings since you last looked (last {MAX_READING_LOG} kept)"
            if self.trimmed:
                # 열 개가 전부라고 읽으면 그 앞을 없었던 일로 세게 된다.
                head += " — earlier readings dropped"
            lines.append(head + ":")
            # 객체가 아니라 **멤버**로 접는다. 같은 애니메이션이 적 다섯에서 돌면 판독마다
            # 대상이 달라 객체로는 안 접히는데, 말하는 내용은 하나다.
            members = {k.rsplit("|", 1)[-1] for e in window for k in e.changed}
            if len(window) > 2 and 0 < len(members) <= 2:
                said = ", ".join(sorted(members))
                where = {k.rsplit("|", 1)[0].rsplit("/", 1)[-1] for e in window for k in e.changed}
                lines.append(
                    f"  {len(window)} readings, only {said} moved"
                    + (f" (on {len(where)} objects)" if len(where) > 1 else "")
                )
            else:
                for entry in window:
                    lines.append(f"  {entry.reading} ({self._log_line(entry)})")
            lines.append("")

        # statics 는 **매번 전부** 그린다. 이 절만 창(window)을 따르지 않는다.
        #
        # 소유자가 없기 때문이다. 객체 멤버는 그 객체가 아래에 그려지면서 함께 보이지만,
        # static 은 어느 객체 아래에도 안 실린다 — 델타에서 빠지는 순간 화면 어디에도 없다.
        #
        # 그리고 static 으로 뽑히는 값이 대체로 **래칭**이다. 한 번 켜지면 꺼질 때까지 유지되고,
        # 그 사이 내내 무엇을 할 수 있는지를 결정한다. 샘플 게임의 `InteractionLock.IsLocked` 가
        # 그렇다: 대화창이 떠 있는 동안 카드 드래그가 통째로 취소되는데, 그 사실이 잠기는 순간
        # 한 번만 보이고 사라졌다. 다음 턴의 에이전트는 잠긴 줄 모르고 끌었고, 카드가 튕기는
        # 것을 좌표가 틀린 것으로 읽었다(ARTEL-573).
        #
        # 값이 열한 개다(실측). 창이 아끼는 것과 견줄 크기가 아니다.
        if self.statics:
            lines.append("statics:")
            for key in sorted(self.statics):
                entry = self.statics[key]
                name = f"{(entry.declaring or '').split('.')[-1]}.{entry.member}"
                # 마지막으로 본 뒤 바뀐 것만 표시한다. 전부 그리면서 표시까지 없으면 읽는 쪽이
                # 무엇이 소식인지 스스로 찾아야 하고, 그것이 창이 하라고 있는 일이다.
                news = "  (changed)" if self.static_at.get(key, 0) > since else ""
                lines.append(f"  {name} = {entry.value!r}{news}{self._moved(key)}")

        objects = sorted(self.held.items())
        for key, obj in objects:
            # 꺼진 것은 그리지 않는다. 화면에 없고 누를 수도 없어 조준 후보가 아니고,
            # GAME_STATE 도 활성 GameObject 만 보냈다 — 여기서 그리면 그것보다 못해진다.
            #
            # 들고 있기는 한다. 판독이 "그건 꺼졌다" 고 말한 것 자체가 사실이고, 다시 켜지면
            # 판독이 그 객체를 active 통에 넣어 보내므로 저절로 돌아온다. held 에서 지우면
            # 그 사이 값을 잃고 다시 켜질 때 전량 판독을 기다리게 된다.
            #
            # 풀에서 꺼내 쓰는 게임이 이것으로 산다: 카드 스무 장짜리 풀에서 손에 든 셋만
            # 활성이면, 프롬프트에 드는 것도 셋이다.
            if not obj.live:
                continue
            # 멤버가 없어도 쓴다. 판독이 유일한 출처일 때, 조준값과 무엇을 할 수 있는지만
            # 들고 오는 객체가 대다수다 — 그것을 버리면 누를 것이 화면에서 사라진다.
            if not obj.members and not obj.offers and obj.id is None:
                continue

            fresh = [k for k in sorted(obj.members) if obj.at.get(k, 0) > since]
            # 조작할 수 있다는 것은 **무엇을 할지 아는 것**이다. id 는 거의 모든 객체에
            # 실리므로 그것으로 가르면 아무것도 안 걸러진다 — offers 가 그 선이다.
            actionable = bool(obj.offers)

            # 이번 창에 아무 말도 없고 누를 수도 없는 객체는 건너뛴다. 독자가 이미 아는
            # 것을 다시 적는 자리다.
            if not fresh and not actionable:
                continue

            where = obj.selector or obj.path or key
            lines.append(f"{where}{self._aim(obj)}:")
            offered = self._offered(obj)
            if offered:
                lines.append(f"  {offered}")
            for member_key in fresh:
                member = obj.members[member_key]
                name = f"{(member.on or '').split('.')[-1]}.{member.member}"
                asked = "" if member.asked is not False else " (unasked)"
                moved = self._moved(f"{key}|{member_key}", f"{where}|{member_key}")
                lines.append(f"  {name} = {member.value!r}{asked}{moved}")

        lines.append(PULSE_VIEW_END)
        return "\n".join(lines)

    def inspect(self, selector: str) -> str:
        """한 객체가 쥔 값 전부. 창과 무관하게 지금 아는 것을 다 적는다.

        상시 블록이 변화와 조작 가능한 것만 싣게 되면서 생긴 짝이다. 안 움직인 값은 매 턴
        적지 않되, 물으면 답할 수 있어야 한다 — 그러지 않으면 줄인 것이 아니라 잃은 것이다.

        부분 일치를 받는 이유: 블록에 적히는 주소는 `RangedCat(Clone)[17]` 처럼 길고, 대괄호
        안 숫자는 씬을 다시 걸을 때마다 달라질 수 있다. 정확히 옮겨 적기를 요구하면 그 자체가
        왕복을 만든다.
        """
        needle = (selector or "").strip().lower()
        if not needle:
            return "Name the object you want to inspect."

        hits = [
            (key, obj)
            for key, obj in sorted(self.held.items())
            if needle in (obj.selector or "").lower() or needle in (obj.path or "").lower()
        ]
        if not hits:
            return (
                f"No object matching {selector!r}. The scene block lists what is there; "
                "the address printed beside each one is what this takes."
            )

        lines = []
        for key, obj in hits[:MAX_INSPECTED]:
            where = obj.selector or obj.path or key
            state = "" if obj.live else "  (switched off)"
            lines.append(f"{where}{self._aim(obj)}{state}:")
            offered = self._offered(obj)
            if offered:
                lines.append(f"  {offered}")
            if not obj.members:
                lines.append("  (no values are being read on this object)")
            for member_key in sorted(obj.members):
                member = obj.members[member_key]
                name = f"{(member.on or '').split('.')[-1]}.{member.member}"
                asked = "" if member.asked is not False else " (unasked)"
                lines.append(f"  {name} = {member.value!r}{asked}")
        if len(hits) > MAX_INSPECTED:
            # 잘랐다는 것을 말한다. 조용히 자르면 독자가 이것을 전부로 읽는다.
            lines.append(f"({len(hits) - MAX_INSPECTED} more objects match; name one more exactly)")
        return "\n".join(lines)

    @staticmethod
    def _aim(obj: "_HeldObject") -> str:
        """조준에 필요한 것: 무엇으로 지목하고 화면 어디인가.

        selector 는 사람이 읽는 주소이고 액션이 받는 것은 아직 id 다. 둘을 함께 쓰는 것이
        ARTEL-480 이 끝나기 전까지의 정직한 모양이다 — 하나만 주면 독자가 나머지를 물어야 한다.
        """
        bits = []
        if obj.id is not None:
            bits.append(f"id={obj.id}")

        # 모서리가 아니라 **중심**을 낸다. 판독이 싣는 `rect.x/y` 는 요소의 좌상단이고,
        # 포인터 도구가 받는 것은 겨눌 점이다. 그대로 내면 276px 짜리에서 138px 씩
        # 어긋나고, 읽는 쪽은 그것이 모서리인 줄 모르므로 어긋난 자리에서 드래그를
        # 시작한다(ARTEL-569).
        #
        # 표기도 `@` 로 맞춘다. `app/qa/scene.py` 의 `_where` 가 같은 뜻을 같은 모양으로
        # 내고 있었고, 여기만 `at` 이었다. 두 줄이 같은 것을 뜻하는 척하면서 다른 것을
        # 뜻하는 상태였다.
        rect = obj.rect or {}
        corner = [rect.get(k) for k in ("x", "y", "w", "h")]
        if all(isinstance(value, (int, float)) for value in corner):
            x, y = center_of(*corner)
            bits.append(f"@ {x},{y} {int(corner[2])}x{int(corner[3])}")
        return f"  [{' · '.join(bits)}]" if bits else ""

    @staticmethod
    def _offered(obj: "_HeldObject") -> str:
        """이 객체에 무엇을 할 수 있나.

        스캔이 볼 수 없는 둘이 여기 있다(`WatchList.Offer`) — 어떤 키가 뜻을 가지는가, 그리고
        어떤 객체가 포인터에 답하는가. 배선된 메서드 이름까지 함께 쓴다: 누른 뒤 아무것도
        움직이지 않았을 때, 무엇이 불렸어야 하는지를 아는 독자만 그것을 결함으로 부를 수 있다.
        """
        offers = obj.offers or {}
        parts = []
        clicks = offers.get("clicks") or []
        for call in clicks:
            on = call.get("on") or "?"
            method = call.get("method") or "?"
            parts.append(f"click → {on}.{method}")
        keys = offers.get("keys") or []
        if keys:
            parts.append("keys: " + ", ".join(_key_line(k) for k in keys))
        pointers = offers.get("pointers") or []
        if pointers:
            parts.append("pointer: " + ", ".join(str(p) for p in pointers))
        return "can do — " + " · ".join(parts) if parts else ""

    @staticmethod
    def _log_line(entry: ReadingLog) -> str:
        """로그 한 줄의 본문.

        전량 판독은 키를 이름 대지 않는다. 직전이 없어 **감시 중인 전부**가 움직인 것으로
        들어오므로, 그 목록은 "무엇이 움직였나"에 답하지 않고 "무엇을 보고 있나"에 답한다 —
        그것은 이 줄이 묻는 것이 아니다.
        """
        if entry.whole:
            return f"whole — {entry.moved} values reported"
        if not entry.moved:
            return "delta — nothing moved"
        named = ", ".join(entry.changed)
        rest = entry.moved - len(entry.changed)
        return f"delta — {named}" + (f", +{rest} more" if rest > 0 else "")

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
