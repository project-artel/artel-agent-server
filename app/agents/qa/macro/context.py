"""`decide(...)` 와 `ask(...)` 의 keyword 인자를 Jev 요청 `state` 로 푼다.

agent 가 적은 keyword 하나가 `state` 의 key 하나다. 값은 그 자리에서 지금의 memory 로
읽는다 — reader 와 같은 길(`binding.resolve_target`, `binding.resolve_find_all`)로 푸므로,
조준이 안 잡히면 reader 와 같은 `MacroFailure` 로 멈춘다.

**값 하나와 전체를 따로 자른다.** 값 하나는 JSON 으로 적어 `MAX_CONTEXT_VALUE_CHARS` 를
넘으면 잘린 문자열로 바꾼다(`relevance._render_value` 와 같은 규칙). 전체가
`MAX_DECIDE_STATE_CHARS` 를 넘으면 목록 안의 object 를 뒤에서부터 빼고 뺀 수를 돌려준다.
runner 가 그 수를 결정 줄에 적어, Jev 가 무엇을 못 보고 골랐는지 agent 가 읽을 수 있다.
"""

import json
from typing import Any

from app.agents.qa.macro.binding import (
    FoundObject,
    LateSelector,
    MacroMemories,
    MacroScope,
    resolve_find_all,
    resolve_target,
)
from app.agents.qa.macro.grammar import MAX_CONTEXT_VALUE_CHARS, MAX_DECIDE_STATE_CHARS
from app.qa.pulse import UnreadValue
from app.agents.qa.macro.model import (
    MacroContextArgument,
    MacroContextFindAll,
    MacroContextLiteral,
    MacroContextName,
    MacroContextTarget,
    MacroContextUnder,
    MacroNameTarget,
    MacroSelectorTarget,
)

# object 목록을 따로 적지 않은 `decide`·`ask` 에서 live object 전부가 들어가는 key.
DEFAULT_OBJECTS_KEY = "objects"


def build_context_state(
    memories: MacroMemories,
    scope: MacroScope,
    context: tuple[MacroContextArgument, ...],
) -> tuple[dict[str, Any], int]:
    """`(state, clipped)`. `clipped` 는 전체 상한 때문에 뺀 object 수다.

    `scene` 은 언제나 들어간다. keyword 가 하나도 없으면 `objects` 에 live object 전부가
    들어간다.
    """
    state: dict[str, Any] = {"scene": memories.scene_name}
    if not context:
        state[DEFAULT_OBJECTS_KEY] = _every_live_object(memories)
    for argument in context:
        state[argument.key] = _argument_value(memories, scope, argument)
    clipped = _fit(state)
    return state, clipped


def _argument_value(
    memories: MacroMemories, scope: MacroScope, argument: MacroContextArgument
) -> Any:
    if isinstance(argument, MacroContextTarget):
        return render_object(memories, resolve_target(memories, scope, argument.target))
    if isinstance(argument, MacroContextFindAll):
        return [
            render_object(memories, found)
            for found in resolve_find_all(memories, scope, argument.find)
        ]
    if isinstance(argument, MacroContextUnder):
        return _objects_under(memories, scope, argument)
    if isinstance(argument, MacroContextName):
        bound = scope.value(argument.name)
        if isinstance(bound, (FoundObject, LateSelector)):
            # object 를 든 이름이면 reader 와 같은 길로 지금의 기록을 다시 읽는다. 그래야
            # 사라진 object 가 `STALE_BINDING` 으로 멈추고, member 값이 지금 값이 된다.
            found = resolve_target(memories, scope, MacroNameTarget(name=argument.name))
            return render_object(memories, found)
        return clip_value(bound)
    assert isinstance(argument, MacroContextLiteral)
    return clip_value(argument.literal.value)


def _objects_under(
    memories: MacroMemories, scope: MacroScope, argument: MacroContextUnder
) -> list[dict[str, Any]]:
    """경로가 `root` 와 같거나 `root/` 로 시작하는 live object 전부. 경로 순서다.

    경로 문자열이면 그대로 쓰고, object 이름이면 reader 와 같은 길로 지금의 기록을 읽어 그
    경로를 쓴다. 경로 문자열은 그 자리에 object 가 없어도 거절하지 않는다 — 아래에 아무것도
    없으면 빈 목록이고, 그것도 Jev 가 읽을 사실이다.
    """
    if isinstance(argument.root, MacroSelectorTarget):
        root = argument.root.selector
    else:
        found = resolve_target(memories, scope, argument.root)
        root = (found.record.selector or found.record.path or "").strip("/")
    memory = memories.pulse
    below: list[dict[str, Any]] = []
    for key in sorted(memory.held):
        record = memory.held[key]
        if not record.live:
            continue
        path = (record.selector or record.path or "").strip("/")
        if path == root or path.startswith(root + "/"):
            below.append(render_object(memories, FoundObject(key=key, record=record)))
    return below


def _every_live_object(memories: MacroMemories) -> list[dict[str, Any]]:
    memory = memories.pulse
    return [
        render_object(memories, FoundObject(key=key, record=memory.held[key]))
        for key in sorted(memory.held)
        if memory.held[key].live
    ]


def render_object(memories: MacroMemories, found: FoundObject) -> dict[str, Any]:
    """object 하나를 `{"path", "text"?, "members"}` 로. cosmetic 으로 판정된 member 는 뺀다.

    member 이름은 `member(<target>, "Type.member")` 가 받는 짧은 이름과 같은 꼴이다. agent 가
    macro 에 적는 글자와 Jev 가 읽는 글자가 같아야 한다.
    """
    record = found.record
    members: dict[str, Any] = {}
    shown = [m for m in record.members.values() if not memories.pulse.is_cosmetic(m.type_key)]
    short_names = [f"{(m.on or '').split('.')[-1]}.{m.member}" for m in shown]
    for member, short in zip(shown, short_names):
        # 같은 type 의 component 가 둘이면 짧은 이름이 겹친다. 그때는 둘 다 `#among` 을 붙여 적는다 —
        # `binding._member_names` 가 붙이지 않은 이름을 모호로 거절하는 것과 같은 규칙이고, Jev 와
        # macro 가 같은 값을 읽게 한다(ARTEL-1289). 전에는 Jev 가 첫 값, macro 가 마지막 값이었다.
        name = f"{short}#{member.among or 0}" if short_names.count(short) > 1 else short
        members[name] = clip_value(_observed_text(member.observed))
    rendered: dict[str, Any] = {"path": record.selector or record.path or found.key}
    if record.text is not None:
        rendered["text"] = clip_value(record.text)
    rendered["members"] = {name: members[name] for name in sorted(members)}
    # adapter 조각은 그대로 싣는다. Jev 가 `interactable`·`playing` 같은 상태를 읽는 자리다.
    for fragment_name, fragment in record.fragments().items():
        rendered[fragment_name] = clip_value(fragment)
    return rendered


def _observed_text(value: Any) -> Any:
    """`UnreadValue` 는 JSON 으로 못 나가므로 글자로. 그 밖은 그대로."""
    if isinstance(value, UnreadValue):
        return f"(unread: {value.reason})"
    return value


def clip_value(value: Any) -> Any:
    """JSON 으로 적어 `MAX_CONTEXT_VALUE_CHARS` 를 넘으면 잘린 문자열로 바꾼다.

    안 넘으면 값을 그대로 둔다. JSON 으로 못 적는 값은 `str` 로 적은 문자열이 된다.
    """
    try:
        text = json.dumps(value, ensure_ascii=False)
        serializable = True
    except (TypeError, ValueError):
        text = str(value)
        serializable = False
    if len(text) > MAX_CONTEXT_VALUE_CHARS:
        return text[: MAX_CONTEXT_VALUE_CHARS - 1] + "…"
    return value if serializable else text


def _length(state: dict[str, Any]) -> int:
    return len(json.dumps(state, ensure_ascii=False, default=str))


def _fit(state: dict[str, Any]) -> int:
    """전체가 `MAX_DECIDE_STATE_CHARS` 안에 들 때까지 object 를 뒤에서부터 뺀다.

    빼는 것은 목록 안의 object 뿐이다(`find_all` 과 `under` 의 결과, 그리고 `objects`). 하나를 가리킨
    keyword 와 이름에 bind 된 값은 agent 가 직접 적은 것이라 남긴다. 맨 뒤 key 의 목록부터
    비우고, 비면 그 앞 key 의 목록으로 간다. 목록을 다 비워도 넘으면 그대로 둔다.
    """
    clipped = 0
    lists = [value for value in state.values() if isinstance(value, list)]
    while _length(state) > MAX_DECIDE_STATE_CHARS:
        non_empty = [objects for objects in lists if objects]
        if not non_empty:
            break
        non_empty[-1].pop()
        clipped += 1
    return clipped
