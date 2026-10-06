"""`find` 와 `selector` 를 `PulseMemory` 하나로 풀고, 도착한 값을 비교한다.

경로 selector 로는 런타임에 spawn 되는 element(손패의 카드 등)를 안정적으로 가리킬 수
없다. `find(label=...)` 가 그 자리를 메우고, 서버가 이미 들고 있는 `PulseMemory` 에
대고 조건을 풀어 기록으로 바꾼다.

**`PulseMemory` 가 유일한 출처다.** `find` 도 `selector` 도 같은 memory, 같은 기록에서
푼다. SDK 는 한 줄도 안 고친다. `GAME_STATE` 채널은 기본이 꺼져 있어(`SendsGameState`
의 기본값이 `false`) `SceneMemory` 에 걸면 기본 빌드에서 한 번도 안 돈다 — 실측 런
하나가 `GAME_STATE` 0장, `PULSE` 14489장을 기록했다. 여기서 `SceneMemory` 를 읽는
것은 `observable()` 하나이고, 그것은 애초에 `GAME_STATE` 전용 reader 다.

**묶인 이름이 드는 값은 id 하나가 아니라 기록 자체다.** 그 기록이 `id` 와 `selector` 를
둘 다 들고 있어, 뒤이은 action 은 어느 쪽으로도 겨눌 수 있다.

코드 넷이 여기서 갈린다.

- `SELECTOR_NOT_FOUND` — macro 텍스트에 적힌 조회가 지금 아무것도 안 잡는다.
- `SELECTOR_AMBIGUOUS` — 둘 이상 잡혔다. 서버가 임의로 하나를 고르면 그것은 서버가
  지어낸 것이 된다.
- `STALE_BINDING` — 이 런에서 몇 statement 전에 푼 값이 그 사이에 죽었다. 앞엣것과
  섞으면 agent 가 할 일이 뒤바뀐다 — 이것은 다시 부르면 되고 앞엣것은 정의를 고쳐야 한다.
- `ACTION_REJECTED` — 읽을 자리가 아예 없다. pulse 를 한 장도 못 받았거나, 적힌 이름이
  그 기록에 없다.

그리고 비교의 거절이 `COMPARISON_REJECTED` 다. 게임이 틀린 것이 아니라 macro 가 못
묻는 것을 물은 것이라 `REQUIRE_FAILED` 와 같은 코드에 담을 수 없다.
"""

from dataclasses import dataclass
from typing import Any

from app.agents.qa.macro.errors import (
    ACTION_REJECTED,
    COMPARISON_REJECTED,
    SELECTOR_AMBIGUOUS,
    SELECTOR_NOT_FOUND,
    STALE_BINDING,
    MacroFailure,
)
from app.agents.qa.macro.grammar import (
    MacroOperator,
    MacroShape,
    allowed_operators,
    comparison_allowed,
    operator_names,
)
from app.agents.qa.macro.model import (
    MacroComparisonCondition,
    MacroCondition,
    MacroFindValue,
    MacroLiteral,
    MacroNameTarget,
    MacroOperand,
    MacroReaderCall,
    MacroSelectorTarget,
    MacroStringRef,
    MacroTarget,
)

# `PulseMemory.held` 가 드는 기록 그 자체다. 밑줄로 시작하지만 `held` 를 읽는 쪽은
# 반드시 이 타입을 다루게 되고, 여기서 비슷한 모양을 하나 더 만들면 같은 사실이 어긋날
# 자리가 둘이 된다.
from app.qa.pulse import PulseMemory, _HeldObject
from app.qa.scene import SceneMemory

# `unread` 가 실려 온 값. SDK 의 `Number` 가 NaN 과 Infinity 를 이렇게 바꿔 보낸다.
# 숫자 자리에 숫자 아닌 모양이 오는 것이고, 이것을 `==` 로 비교해 조용히 거짓이 되면
# `require` 가 게임 결함처럼 실패한다.
UNREAD = "unread"


@dataclass(frozen=True)
class FoundObject:
    """`find` 나 `selector` 가 푼 pulse 기록 하나.

    `key` 는 `PulseMemory` 가 객체를 세는 키(`씬/(selector 또는 path)`)다. 같음의 기준도
    이것이다 — `PulseObject.id` 가 `int | None` 이라 `id` 로 가르면 `None` 인 기록에
    규칙이 없고, `PulseMemory` 자신이 객체를 가르는 기준이 이 키다.
    """

    key: str
    record: _HeldObject


@dataclass(frozen=True)
class LateSelector:
    """`selector(...)` 를 묶은 이름이 드는 것. 그 자리에서 풀지 않는다.

    즉시 풀면 적어 둔 주소인데도 `STALE_BINDING` 이 나서, 적어 둔 주소면
    `REQUIRE_FAILED`, 이 런에서 찾은 값이면 `STALE_BINDING` 이라는 기준이 깨진다.
    `find(...)` 를 묶은 이름과 다른 점이 이것이다.
    """

    selector: str


@dataclass(frozen=True)
class MacroMemories:
    """macro 가 읽는 것 전부. `observable()` 하나만 `SceneMemory` 쪽을 본다."""

    scene: SceneMemory

    @property
    def pulse(self) -> PulseMemory:
        return self.scene.pulse

    @property
    def scene_name(self) -> str:
        """지금 서 있는 scene 이름. `pulse` 에서도 읽는다.

        `GAME_STATE` 없이 `pulse` 만 오는 게임에서는 `SceneMemory.scene` 이 끝까지
        비어 있다 — `_standing_scene` 이 같은 이유로 같은 두 자리를 본다.
        """
        return (self.scene.scene or self.pulse.scene or "").strip()


class MacroScope:
    """한 `def` 가 도는 동안 맨이름이 드는 값.

    `def` 하나가 scope 하나다. parser 가 한 `def` 안에서 이름을 한 번만 묶게 했으므로,
    여기서 덮어쓰기를 걱정할 자리가 없다.
    """

    def __init__(self, arguments: dict[str, Any] | None = None) -> None:
        self.values: dict[str, Any] = dict(arguments or {})

    def bind(self, name: str, value: Any) -> None:
        self.values[name] = value

    def value(self, name: str) -> Any:
        if name not in self.values:
            # parser 가 묶이지 않은 이름을 이미 거절하므로, 여기 닿으면 runner 와 parser
            # 가 같은 몸통을 다르게 걸은 것이다.
            raise MacroFailure(
                ACTION_REJECTED,
                f"`{name}` has no value at this point in the macro.",
                {"name": name, "bound": sorted(self.values)},
            )
        return self.values[name]

    def text(self, reference: MacroStringRef) -> str:
        """문자열 하나가 오는 자리를 지금의 값으로 푼다."""
        if reference.kind == "literal":
            return reference.value
        bound = self.value(reference.name)
        return bound if isinstance(bound, str) else str(bound)


# --- `find` 와 `selector` -------------------------------------------------------


def _require_readable(memory: PulseMemory) -> None:
    """읽을 자리가 아예 없을 때 하나만 `ACTION_REJECTED` 로 남는다."""
    if memory.seen:
        return
    raise MacroFailure(
        ACTION_REJECTED,
        "the game has not sent a single pulse reading yet, so there is nothing to "
        "resolve a target against. Observe the scene and call the macro again.",
    )


def _live(memory: PulseMemory) -> list[tuple[str, _HeldObject]]:
    """지금 켜져 있는 기록만.

    꺼진 것을 빼는 이유는 `PulseMemory.render` 가 그것을 안 그리는 이유와 같다 — 화면에
    없고 누를 수도 없어 조준 후보가 아니다. 넣으면 `find` 가 agent 가 본 적 없는 것을
    돌려주게 된다. 그래서 묶은 뒤 그 객체가 꺼지면 `STALE_BINDING` 이 나고, 그것이 이
    경우에 agent 가 할 일(다시 부른다)과 맞는다.
    """
    return [(key, record) for key, record in memory.held.items() if record.live]


def _segments(record: _HeldObject) -> set[str]:
    """`name=` 이 맞출 수 있는 이름들.

    selector 마지막 세그먼트와 `path` 를 맞춘다. 대괄호 안 번호는 씬을 다시 걸을 때마다
    달라질 수 있으므로 번호를 뗀 모양도 함께 받는다 — 번호까지 옮겨 적기를 요구하면 그
    자체가 왕복을 만든다. 부분 일치는 아니다. 어느 모양이든 글자가 정확히 같아야 한다.
    """
    names: set[str] = set()
    for address in (record.selector, record.path):
        if not address:
            continue
        names.add(address)
        last = address.rsplit("/", 1)[-1]
        names.add(last)
        if last.endswith("]") and "[" in last:
            names.add(last[: last.rindex("[")])
    return names


def _under(key: str, scene: str, under: str) -> bool:
    """`under=` 가 그 기록을 범위에 넣는가. memory 의 key 에 prefix 로 맞춘다.

    key 는 씬 이름 뒤에 `selector`(없으면 `path`)를 붙인 전체 경로다. 그런데 agent 가
    화면에서 읽는 주소에는 씬 이름이 안 붙어 있으므로, 씬을 안 적은 prefix 도 받는다 —
    안 받으면 `under="Root[0]/Hand[2]"` 가 언제나 아무것도 안 맞춘다.

    **마디 경계에서 끊는다.** 글자 prefix 로만 보면 `under="Root[0]/Hand"` 가
    `Root[0]/HandSlot[3]/...` 까지 끌고 오고, 더 나쁘게는 씬 이름의 앞글자가 그 씬의
    모든 객체를 맞춘다 — `scene="TurnBattleScene"` 에서 `under="T"` 가 전부 통과한다.
    그러면 걸린 것이 하나뿐일 때 범위 제한이 조용히 없는 것이 되고, macro 는 범위를
    좁힌 것처럼 읽힌다.
    """
    return any(
        candidate == under or candidate.startswith(f"{under.rstrip('/')}/")
        for candidate in (key, _without_scene(key, scene))
    )


def _without_scene(key: str, scene: str) -> str:
    """key 에서 씬 이름 마디를 뗀 나머지. 화면에서 읽는 주소가 이 모양이다."""
    prefix = f"{scene}/"
    return key[len(prefix) :] if scene and key.startswith(prefix) else key


def resolve_find(
    memories: MacroMemories, scope: MacroScope, value: MacroFindValue
) -> FoundObject:
    """`find(...)` 를 지금의 `PulseMemory` 에 대고 푼다."""
    memory = memories.pulse
    _require_readable(memory)

    label = None if value.label is None else scope.text(value.label)
    name = None if value.name is None else scope.text(value.name)
    under = None if value.under is None else scope.text(value.under)

    hits: list[FoundObject] = []
    scene = memory.scene or ""
    for key, record in _live(memory):
        if under is not None and not _under(key, scene, under):
            continue
        if label is not None and record.text != label:
            continue
        if name is not None and name not in _segments(record):
            continue
        hits.append(FoundObject(key=key, record=record))

    asked = ", ".join(
        f"{keyword}={given!r}"
        for keyword, given in (("label", label), ("name", name), ("under", under))
        if given is not None
    )
    if not hits:
        # 대상의 첫 판독이 아직 안 왔을 수 있다. macro 정의 자체가 낡은 경우보다 이 관측
        # 순서 문제가 더 흔하다 — scene 전환 직후나 카드를 새로 뽑은 직후가 그 경우다.
        raise MacroFailure(
            SELECTOR_NOT_FOUND,
            f"find({asked}) matched nothing the game has reported. Observe the scene "
            "and call the macro again — the first reading of that object may not have "
            "arrived yet. If it still matches nothing, the macro's own wording is out "
            "of date.",
            {"asked": asked},
        )
    if len(hits) > 1:
        # 서버가 임의로 하나를 고르면 안 된다. 고르는 규칙이 정의에 없으므로 고른 것은
        # 서버가 지어낸 것이 된다.
        raise MacroFailure(
            SELECTOR_AMBIGUOUS,
            f"find({asked}) matched {len(hits)} objects, so the name is not unique and "
            "nothing was sent to the game. Narrow it with `under=`, or with `label=` if "
            "the two show different text.",
            {"asked": asked, "matched": sorted(hit.key for hit in hits)},
        )
    return hits[0]


def _by_selector(memories: MacroMemories, selector: str) -> FoundObject | None:
    memory = memories.pulse
    _require_readable(memory)
    for key, record in _live(memory):
        if record.selector == selector or record.path == selector:
            return FoundObject(key=key, record=record)
    return None


def resolve_target(
    memories: MacroMemories, scope: MacroScope, target: MacroTarget
) -> FoundObject:
    """조준 하나를 지금의 pulse 기록으로 푼다.

    `selector(...)` 는 쓰이는 자리에서 푼다(late binding). 묶인 이름은 그때 푼 기록이
    지금도 memory 에 있는지를 다시 본다 — 없으면 `STALE_BINDING` 이고, 그것은 적어 둔
    주소가 낡은 것과 다른 얘기다.
    """
    if isinstance(target, MacroSelectorTarget):
        found = _by_selector(memories, target.selector)
        if found is None:
            raise MacroFailure(
                SELECTOR_NOT_FOUND,
                f"selector({target.selector!r}) matches nothing the game has reported. "
                "The address written into this macro may be out of date.",
                {"selector": target.selector},
            )
        return found

    bound = scope.value(target.name)
    if isinstance(bound, LateSelector):
        found = _by_selector(memories, bound.selector)
        if found is None:
            raise MacroFailure(
                SELECTOR_NOT_FOUND,
                f"`{target.name}` holds selector({bound.selector!r}), which matches "
                "nothing the game has reported. The address written into this macro may "
                "be out of date.",
                {"name": target.name, "selector": bound.selector},
            )
        return found

    if isinstance(bound, FoundObject):
        fresh = memories.pulse.held.get(bound.key)
        if fresh is None or not fresh.live:
            raise MacroFailure(
                STALE_BINDING,
                f"`{target.name}` was resolved earlier in this run and the object it "
                "held is gone. Nothing was sent to the game. Call the macro again — the "
                "macro's own wording is fine, the object it found is not there any more.",
                {"name": target.name, "key": bound.key},
            )
        return FoundObject(key=bound.key, record=fresh)

    raise MacroFailure(
        ACTION_REJECTED,
        f"`{target.name}` holds {bound!r}, which is not an object to aim at.",
        {"name": target.name},
    )


def _exists(memories: MacroMemories, scope: MacroScope, target: MacroTarget) -> bool:
    """`exists` 와 `absent` 는 답이 참거짓이라 조회 실패로 멈추지 않는다."""
    try:
        resolve_target(memories, scope, target)
    except MacroFailure as failure:
        if failure.code in (SELECTOR_NOT_FOUND, STALE_BINDING):
            return False
        raise
    return True


# --- 조건의 왼쪽에 오는 호출 여덟 -----------------------------------------------


def _static_names(memory: PulseMemory) -> dict[str, Any]:
    """`static("Type.member")` 가 맞출 이름 → 값.

    화면에 그려지는 이름을 그대로 받는다(`PulseMemory.render` 의 statics 절이
    `declaring` 의 마지막 마디와 `member` 를 점으로 붙여 쓴다). agent 가 읽은 글자와
    macro 에 적는 글자가 같아야 하므로, memory 의 내부 키도 함께 받는다.
    """
    named: dict[str, Any] = {}
    for key, entry in memory.statics.items():
        named[key] = entry.value
        short = (entry.declaring or "").split(".")[-1]
        named[f"{short}.{entry.member}"] = entry.value
    return named


def _member_names(record: _HeldObject) -> dict[str, Any]:
    """`member(<target>, "Type.member")` 가 맞출 이름 → 값. statics 와 같은 규칙이다."""
    named: dict[str, Any] = {}
    for key, member in record.members.items():
        named[key] = member.value
        short = (member.on or "").split(".")[-1]
        named[f"{short}.{member.member}"] = member.value
        if member.member:
            # 한 객체에 같은 이름의 멤버가 둘인 경우는 `on` 이 다를 때뿐이다. 그때는
            # 짧은 이름이 겹치므로 먼저 들어온 것이 남고, 긴 이름이 가릴 수 있는 길로
            # 남는다.
            named.setdefault(member.member, member.value)
    return named


def read(memories: MacroMemories, scope: MacroScope, call: MacroReaderCall) -> Any:
    """조건의 왼쪽에 오는 호출 여덟 중 하나를 지금의 memory 에 대고 읽는다."""
    reader = call.reader
    if reader == "scene":
        return memories.scene_name
    if reader == "observable":
        return _observable(memories, scope, call)
    if reader == "static":
        return _static(memories, scope, call)

    # 남은 다섯은 target 을 받는다.
    if reader == "exists":
        return _exists(memories, scope, call.target)
    if reader == "absent":
        return not _exists(memories, scope, call.target)

    found = resolve_target(memories, scope, call.target)
    if reader == "actionable":
        # `offers` 가 그 선이다. `id` 는 거의 모든 객체에 실리므로 그것으로 가르면
        # 아무것도 안 걸러진다.
        return bool(found.record.offers)
    if reader == "text":
        return found.record.text
    if reader == "member":
        return _member(found, scope, call)
    raise MacroFailure(  # pragma: no cover - parser 가 여덟 밖의 이름을 거절한다
        ACTION_REJECTED, f"`{reader}` is not a reader a macro can call."
    )


def _observable(
    memories: MacroMemories, scope: MacroScope, call: MacroReaderCall
) -> Any:
    name = scope.text(call.key)
    track = memories.scene.observables.get(name)
    if track is None:
        raise MacroFailure(
            ACTION_REJECTED,
            f"observable({name!r}) is not something this run has been sent. "
            "`observable()` reads the `GAME_STATE` channel, which is off in a default "
            "build, so a macro written against it answers nothing there — "
            'static("Type.member") is what reads a value on a default build.',
            {"asked": name, "available": sorted(memories.scene.observables)},
        )
    return track.current


def _static(memories: MacroMemories, scope: MacroScope, call: MacroReaderCall) -> Any:
    name = scope.text(call.key)
    named = _static_names(memories.pulse)
    if name not in named:
        raise MacroFailure(
            ACTION_REJECTED,
            f"static({name!r}) is not a static this run has been sent. The statics "
            "block of the scene view lists the names this takes.",
            {"asked": name, "available": sorted(named)},
        )
    return named[name]


def _member(found: FoundObject, scope: MacroScope, call: MacroReaderCall) -> Any:
    name = scope.text(call.key)
    named = _member_names(found.record)
    if name not in named:
        raise MacroFailure(
            ACTION_REJECTED,
            f"member({name!r}) is not a value the game reads on {found.key}. Nothing "
            "was sent to the game.",
            {"asked": name, "object": found.key, "available": sorted(named)},
        )
    return named[name]


# --- 도착한 값의 모양 -----------------------------------------------------------


def shape_of(value: Any) -> MacroShape:
    """표를 찾으려면 도착한 값의 모양을 알아야 한다.

    `PulseMember.value` 는 일부러 `Any` 다. 채널이 스칼라 말고도 참조, sprite, animator
    state, 라벨, 개수를 싣기 때문이고, 스칼라로 좁히면 step 이 가리키는 대상을 싣던
    절반이 떨어진다. 그래서 모양은 도착한 값 자체에서 읽는다 — `PulseMember` 에 선언
    타입을 싣게 하면 SDK 를 고쳐야 한다.

    bool 을 먼저 가린다. Python 에서 `True` 는 int 이기도 하다.
    """
    if isinstance(value, (FoundObject, LateSelector)):
        return MacroShape.object_
    if isinstance(value, bool):
        return MacroShape.bool_
    if isinstance(value, (int, float)):
        return MacroShape.number
    if isinstance(value, str):
        return MacroShape.string
    if isinstance(value, dict):
        if UNREAD in value:
            # `Number` 가 NaN 과 Infinity 를 이렇게 바꿔 보낸다.
            return MacroShape.other
        # 참조는 `path` 와 `world` 로 온다.
        if "path" in value or "world" in value:
            return MacroShape.object_
        if {"x", "y", "z"} <= set(value):
            return MacroShape.vector3
        if {"x", "y"} <= set(value):
            return MacroShape.vector2
    return MacroShape.other


def _object_key(memories: MacroMemories, value: Any) -> str:
    """object 하나를 pulse 키로 적는다. 같음의 기준이 이것이다.

    `id` 로 가르지 않는 이유는 `PulseObject.id` 가 `int | None` 이라 `None` 인 기록에
    규칙이 없기 때문이고, `PulseMemory` 자신이 객체를 가르는 기준이 이 키이기 때문이다.

    **주소는 반드시 memory 를 거쳐 키로 바꾼다.** 주소에서 키를 직접 조립하면 안 된다 —
    memory 의 키는 `selector`(번호가 붙은 전체 경로)로 세워지는데, 멤버에 실려 오는
    참조는 `path`(번호가 없는 이름 경로)를 든다. 한 객체가 `path` `Canvas/continue` 와
    `selector` `Canvas[2]/continue[1]` 을 동시에 들고 있으므로, 조립한 키는 같은 객체를
    가리키면서도 영원히 안 맞는다. `_by_selector` 가 둘 다 보고 하나의 키로 답한다.

    못 찾으면 적힌 주소를 그대로 키 자리에 쓴다. 주소 둘을 견주는 것이 그 자체로 답이고,
    없는 것을 조회 실패로 돌리면 `t == selector("...")` 가 거짓 대신 멈추게 된다.
    """
    if isinstance(value, FoundObject):
        return value.key
    address = (
        value.selector
        if isinstance(value, LateSelector)
        else (value.get("path") or "")
        if isinstance(value, dict)
        else str(value)
    )
    found = _by_selector(memories, address)
    return found.key if found else f"{memories.pulse.scene or ''}/{address}"


# --- 비교 ----------------------------------------------------------------------


def compare(
    memories: MacroMemories,
    left: Any,
    operator: MacroOperator,
    right: Any,
) -> bool:
    """`(값의 모양, 연산자)` 표를 찾아 비교하거나 거절한다.

    거절은 `COMPARISON_REJECTED` 다. `REQUIRE_FAILED` 가 아닌 이유는 그것이 조건이
    거짓이라는 뜻이고 agent 가 게임이 다르게 동작한 것으로 읽어 결함을 적기 때문이다.
    연산자 거절은 게임이 틀린 것이 아니라 macro 가 못 묻는 것을 물은 것이다.
    """
    left_shape = shape_of(left)
    right_shape = shape_of(right)

    if left_shape is not right_shape:
        raise MacroFailure(
            COMPARISON_REJECTED,
            f"the two sides of this comparison arrived as different shapes: "
            f"{left_shape.value} on the left ({left!r}) and {right_shape.value} on the "
            f"right ({right!r}). Nothing was sent to the game. A comparison across "
            "shapes would be false forever, which reads as a broken game rather than a "
            "broken macro.",
            {
                "left_shape": left_shape.value,
                "right_shape": right_shape.value,
                "operator": operator.value,
            },
        )

    if not comparison_allowed(left_shape, operator):
        allowed = allowed_operators(left_shape)
        raise MacroFailure(
            COMPARISON_REJECTED,
            f"`{operator.value}` cannot be used on a {left_shape.value}, and the value "
            f"that arrived is one ({left!r}). Nothing was sent to the game. On a "
            f"{left_shape.value} a macro can use: {operator_names(allowed)}.",
            {
                "shape": left_shape.value,
                "operator": operator.value,
                "allowed": [one.value for one in allowed],
                "arrived": repr(left),
            },
        )

    if left_shape is MacroShape.object_:
        same = _object_key(memories, left) == _object_key(memories, right)
        return same if operator is MacroOperator.equal else not same

    return _apply(operator, left, right)


def _apply(operator: MacroOperator, left: Any, right: Any) -> bool:
    if operator is MacroOperator.equal:
        return bool(left == right)
    if operator is MacroOperator.not_equal:
        return bool(left != right)
    if operator is MacroOperator.greater:
        return bool(left > right)
    if operator is MacroOperator.less:
        return bool(left < right)
    if operator is MacroOperator.greater_or_equal:
        return bool(left >= right)
    return bool(left <= right)


# --- 조건 하나 -----------------------------------------------------------------


def operand_value(
    memories: MacroMemories,
    scope: MacroScope,
    operand: MacroOperand,
    observed: dict[str, Any] | None = None,
) -> Any:
    """피연산자 하나의 지금 값.

    `observed` 를 주면 읽은 호출의 값을 거기 적는다. `flag` 와 `ask_verdict` 에 붙는
    `observed` 가 이 기록이고, 값을 두 번 읽지 않으려고 평가와 같은 걸음에서 모은다.
    """
    if operand.kind == "literal":
        return _literal_value(operand.literal)
    if operand.kind == "name":
        return scope.value(operand.name)
    if operand.kind == "selector":
        return LateSelector(selector=operand.selector)
    value = read(memories, scope, operand.call)
    if observed is not None:
        observed[describe(operand.call)] = value
    return value


def _literal_value(literal: MacroLiteral) -> Any:
    return literal.value


def readers_in(condition: MacroCondition) -> list[MacroReaderCall]:
    """그 조건이 읽는 호출 전부. `observed` 를 채우는 자리가 이것을 쓴다."""
    if condition.kind == "call":
        return [condition.call]
    return [
        operand.call
        for operand in (condition.left, condition.right)
        if operand.kind == "reader"
    ]


def describe(call: MacroReaderCall) -> str:
    """읽은 호출 하나를 사람이 읽는 한 조각으로. `observed` 의 키가 된다."""
    parts: list[str] = []
    if call.target is not None:
        parts.append(
            call.target.selector
            if isinstance(call.target, MacroSelectorTarget)
            else call.target.name
        )
    if call.key is not None:
        parts.append(
            call.key.value if call.key.kind == "literal" else call.key.name
        )
    return f"{call.reader}({', '.join(parts)})"


def evaluate(
    memories: MacroMemories,
    scope: MacroScope,
    condition: MacroCondition,
    observed: dict[str, Any] | None = None,
) -> bool:
    """`require` 와 `if` 의 조건을 판정한다. 평가기가 하나다.

    파서가 `if` 의 조건 문법을 `require` 의 첫 인자와 같게 두었으므로, 평가 시점의
    연산자 거절도 한 곳에서 난다. 거절된 `if` 조건을 거짓으로 읽어 분기를 조용히 넘기면
    안 된다.

    `observed` 를 주면 이 조건이 읽은 호출의 값을 거기 적는다.
    """
    if isinstance(condition, MacroComparisonCondition):
        left = operand_value(memories, scope, condition.left, observed)
        right = operand_value(memories, scope, condition.right, observed)
        return compare(memories, left, condition.operator, right)

    value = read(memories, scope, condition.call)
    if observed is not None:
        observed[describe(condition.call)] = value
    shape = shape_of(value)
    if shape is MacroShape.bool_:
        return bool(value)
    # 비교 없는 조건은 참거짓으로 도착해야 한다. 그 밖의 모양을 진리값으로 접으면
    # 빈 문자열이 거짓, 아무 사전이 참이 되어 조용히 틀린다 — `require` 가 게임 결함처럼
    # 실패하는 바로 그 경우다. `static()`·`observable()`·`member()` 셋만 여기 닿는다.
    raise MacroFailure(
        COMPARISON_REJECTED,
        f"{describe(condition.call)} arrived as a {shape.value} ({value!r}), and a "
        "condition written without a comparison has to arrive as a bool. Nothing was "
        "sent to the game. Compare it instead: "
        f"`{describe(condition.call)} == <value>`.",
        {
            "shape": shape.value,
            "arrived": repr(value),
            "reader": condition.call.reader,
        },
    )
