"""executer 가 action 을 고를 때 Jev 에게 보내는 state 를 거른다(`jev_state="members"`).

원본 state(`build_context_state`)는 화면의 live object 를 경로 알파벳순으로 담고, 6000자를 넘으면 뒤에서
뺀다. 전투 씬에서는 그 상한 때문에 판단에 필요한 object 가 빠졌다. 로컬 try 171 의 전투 turn 에는 live
object 가 28개쯤 있었는데 Jev 에게는 8개만 갔다. 남은 자리는 `DontDestroyOnLoad/*` 와 member 가 많은
`TutorialController`(약 3200자)가 차지했다. 다른 object 를 가리키는 member 는 80자에서 잘려 `active` 까지 잃었다.

여기서는 같은 화면을 이렇게 줄인다.

1. 참조 member 를 자르기 전에 `{path, active, label}` 로 줄인다.
2. 선택지에 오른 action 과 판단 macro 가 읽는 object(`Reads`: 계획 source 와 developer macro 몸통의
   `selector`·`find`·`find_all`·`member` 문자열)는 고정한다.
   나머지 object 는 Jev 에게 "이 step 의 다음 action 이나 목표 확인에 필요한가" 를 noul 로 묻는다.
   `OBJECT_KEEP_AT` 미만은 뺀다.
3. member 가 `MEMBER_HEAVY` 개보다 많은 object 는 member 마다 같은 질문을 하고, `MEMBER_KEEP_AT` 미만은
   뺀다. 계획이 `member(...)` 로 읽는 member 는 고정한다.
4. 고정한 것을 먼저, 그다음 확률이 높은 순으로 놓는다. feature·static 을 보태고 상한에 맞추는 것은 모든 경로가
   함께 쓰는 `assemble` 이다.

답은 계획 하나와 경로(`[n]` 자리 번호를 뗀 것)마다 한 번만 묻고 저장한다. 같은 계획 안의 clone 은 한 번이다.
Jev 가 답하지 못하면 남긴다. 거른 결과가 비면 원본으로 돌아간다. 근거 없이 고르게 하지 않기 위해서다.

try 171 기록을 그대로 다시 물어 본 오프라인 비교가 있다. 전투 70 turn 에서 Claude 오라클과 맞은 선택이
원본은 33, 이 방식은 58이었다. 오브젝트 단위로만 거르는 방식은 23이었다. 그 방식은 `ActionHint/HintText`
를 70 turn 모두에서 뺐다. 목표 확인(`goal_ask`)은 표본이 작아 판단하지 못했으므로, 이 state 는 action
고르기에만 쓴다.
"""

import ast
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.agents.planner_executer.macros import StepMacros
from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.macro.context import clip_value
from app.agents.qa.macro.grammar import MAX_DECIDE_STATE_CHARS
from app.agents.qa.macro.model import MacroDefinition
from app.qa.jev import JevClient, JevError
from app.qa.pulse import named_values

OBJECT_KEEP_AT = 0.2
MEMBER_HEAVY = 6
MEMBER_KEEP_AT = 0.3
# Jev 요청 하나에 넣는 질문 수.
QUESTIONS_PER_REQUEST = 40
# 질문 문장에 싣는 값의 길이.
MAX_ASKED_VALUE_CHARS = 120
MAX_ASKED_MEMBERS_CHARS = 300

RELEVANT_MEANS = "the object, or a value on it, tells which action to take now or whether the step's goal is reached"
IRRELEVANT_MEANS = "the object has nothing to do with choosing the next action or checking the goal"

_INDEX = re.compile(r"\[\d+\]")
_REFERENCE_KEYS = ("path", "active", "label")


def bare(path: str) -> str:
    """`Canvas[5]/Button[0]` → `Canvas/Button`."""
    return _INDEX.sub("", path)


@dataclass(frozen=True)
class Reads:
    """선택지에 오른 action 과 그 판단 macro 가 읽는 이름. Jev state 에 반드시 실어야 하는 것이다.

    `objects` 는 `selector`·`find`·`find_all` 의 문자열, `members` 는 `member(...)` 의 이름,
    `statics` 는 `static(...)` 의 이름이다.
    """

    objects: frozenset[str] = frozenset()
    members: frozenset[str] = frozenset()
    statics: frozenset[str] = frozenset()


_OBJECT_READERS = ("selector", "find", "find_all")


def reads_of(sources: Iterable[str]) -> Reads:
    """macro source 들이 읽는 이름. 못 읽는 source 는 건너뛴다."""
    objects: set[str] = set()
    members: set[str] = set()
    statics: set[str] = set()
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            texts = {value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str)}
            if node.func.id in _OBJECT_READERS:
                objects |= texts
            elif node.func.id == "member":
                members |= texts
            elif node.func.id == "static":
                statics |= texts
    return Reads(frozenset(objects), frozenset(members), frozenset(statics))


def definition_sources(definition: MacroDefinition) -> list[str]:
    """macro 하나의 source 와, 그것이 `from macros import` 로 가져온 macro 들의 source."""
    found = [definition.source]
    for imported in definition.imports:
        found.extend(definition_sources(imported))
    return found


def referenced_names(source: str) -> set[str]:
    """계획 source 의 `selector`·`find`·`find_all`·`member` 에 적힌 문자열."""
    reads = reads_of([source])
    return set(reads.objects | reads.members)


def compact_value(value: Any) -> Any:
    """다른 object 를 가리키는 값은 `{path, active, label}` 로 줄이고, 나머지는 원본처럼 자른다."""
    parsed = value
    if isinstance(value, str) and value.startswith("{") and '"path"' in value:
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
    if isinstance(parsed, dict) and "path" in parsed:
        return {key: parsed[key] for key in _REFERENCE_KEYS if key in parsed}
    return clip_value(value)


def compact_objects(memories: MacroMemories) -> list[dict[str, Any]]:
    """`context.render_object` 와 같은 모양으로 live object 전부를 담는다. 참조는 자르기 전에 줄인다."""
    memory = memories.pulse
    objects = []
    for key in sorted(memory.held):
        record = memory.held[key]
        if not record.live:
            continue
        members: dict[str, Any] = {}
        for member in record.members.values():
            if memory.is_cosmetic(member.type_key):
                continue
            short = (member.on or "").split(".")[-1]
            for name, value in named_values(f"{short}.{member.member}", member.value, member.count):
                members.setdefault(name, compact_value(value))
        rendered: dict[str, Any] = {"path": record.selector or record.path or key}
        if record.text is not None:
            rendered["text"] = clip_value(record.text)
        rendered["members"] = {name: members[name] for name in sorted(members)}
        objects.append(rendered)
    return objects


def _pinned(obj: dict[str, Any], names: set[str]) -> bool:
    path, text = bare(obj["path"]), str(obj.get("text") or "")
    last = path.rsplit("/", 1)[-1]
    return any(name in (path, last) or path.endswith("/" + name) or (text and name == text) for name in names)


def _member_pinned(member: str, names: set[str]) -> bool:
    return any(name == member or name.startswith(member + ".") or member.startswith(name) for name in names)


def statics_of(memories: MacroMemories) -> dict[str, Any]:
    """live static 전부를 `static(...)` 이 받는 이름(`Type.member`, 개수는 `.Count`)으로. cosmetic 은 뺀다."""
    memory = memories.pulse
    statics: dict[str, Any] = {}
    for key in sorted(memory.statics):
        if memory.is_cosmetic(key):
            continue
        entry = memory.statics[key]
        short = (entry.declaring or "").split(".")[-1]
        for name, value in named_values(f"{short}.{entry.member}", entry.value, entry.count):
            statics.setdefault(name, compact_value(value))
    return statics


def _static_read(name: str, reads: Reads) -> bool:
    # `static("X.Count")` 도 `X` 를 읽는 것이다.
    return any(name == read or name.startswith(read + ".") or read.startswith(name) for read in reads.statics)


def unfiltered(memories: MacroMemories, reads: Reads) -> list[dict[str, Any]]:
    """거르지 않은 live object. action 이 읽는 object 가 앞이다."""
    objects = compact_objects(memories)
    names = set(reads.objects)
    return [obj for obj in objects if _pinned(obj, names)] + [obj for obj in objects if not _pinned(obj, names)]


def watched_values(memories: MacroMemories, reads: Reads) -> dict[str, Any]:
    """action 이 읽는 값의 지금 모습. action 앞뒤를 비교해 `recent_picks` 의 `changed` 를 만든다.

    읽는 object 는 있는지, 읽는 member 는 그 값, 읽는 static 은 그 값이다.
    """
    objects = compact_objects(memories)
    watched: dict[str, Any] = {}
    for name in sorted(reads.objects):
        matched = [obj for obj in objects if _pinned(obj, {name})]
        watched[f"present:{name}"] = bool(matched)
        for obj in matched[:3]:
            for member, value in obj["members"].items():
                if member in reads.members:
                    watched[f"{bare(obj['path'])}.{member}"] = value
    for name, value in statics_of(memories).items():
        if _static_read(name, reads):
            watched[name] = value
    return watched


def changed_values(before: dict[str, Any], after: dict[str, Any], limit: int) -> dict[str, list[Any]]:
    """앞뒤가 다른 값 `{이름: [앞, 뒤]}`. 많으면 앞에서 `limit` 개."""
    changed = {
        name: [before.get(name), after.get(name)]
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    }
    return dict(list(changed.items())[:limit])


def _reading_members(obj: dict[str, Any], reads: Reads) -> dict[str, Any]:
    """member 가 `MEMBER_HEAVY` 개보다 많으면 action 이 읽는 member 만 남긴 사본."""
    if len(obj["members"]) <= MEMBER_HEAVY:
        return obj
    kept = {name: value for name, value in obj["members"].items() if _member_pinned(name, set(reads.members))}
    return {**obj, "members": kept}


def _size(state: dict[str, Any]) -> int:
    return len(json.dumps(state, ensure_ascii=False, default=str))


def assemble(
    memories: MacroMemories,
    objects: list[dict[str, Any]],
    reads: Reads,
    features: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Jev 에게 보내는 state 하나와 그 요약. 모든 경로(raw·members·목표 확인)가 이것으로 만든다.

    try 193 에서 Jev 는 시전할 수 있는 턴에 턴 종료를 골랐다. 그때 state 에는 static 이 하나도 없었고
    (`CombineZone.Instance.IsAwaitingTarget` 도), 계획에 feature 가 없으면 object 목록만, 있으면 feature
    만 갔다. developer macro 몸통이 읽는 `CardManager`·`CombineZone` 은 고정되지 않아 거르기에서 빠졌다.

    그래서 반드시 싣는 것을 먼저 놓는다: feature, action 이 읽는 object 와 static. 그다음 자리가 남는 만큼
    나머지 static 과 `objects` 의 나머지(주어진 순서)를 싣는다. 상한(`MAX_DECIDE_STATE_CHARS`)을 넘으면
    읽지 않는 object 부터, 그다음 읽지 않는 static 을 뺀다. 그래도 넘으면 읽는 object 를 뒤에서 뺀다.
    action 이 찾는데 화면에 없는 이름은 `not_on_screen` 으로 싣는다. 요약의 `missing` 은 action 이 읽는데 화면에
    있으면서 state 에 못 실은 이름이다.
    """
    names = set(reads.objects)
    # 꼭 싣는 object 라도 member 가 많으면 action 이 읽는 member 만 남긴다. try 193 의 전투 turn 에서 member 15개씩인
    # 카드 8장이 상한을 다 써서 `CardManager`·턴 종료 버튼과 static 이 밀려났다.
    required = [_reading_members(obj, reads) for obj in objects if _pinned(obj, names)]
    rest = [obj for obj in objects if not _pinned(obj, names)]
    # action 이 찾는데 화면에 없는 것. 없다는 것도 판단 근거다 — 조합 버튼이 없으면 준비된 주문이 없다.
    absent = sorted(name for name in names if not any(_pinned(obj, {name}) for obj in objects))
    statics = statics_of(memories)
    read_statics = {name: value for name, value in statics.items() if _static_read(name, reads)}
    other_statics = {name: value for name, value in statics.items() if name not in read_statics}

    def build(objs: list[dict[str, Any]], extra_statics: dict[str, Any]) -> dict[str, Any]:
        state: dict[str, Any] = {"scene": memories.scene_name}
        if features:
            state["features"] = features
        merged = {**read_statics, **extra_statics}
        if merged:
            state["statics"] = merged
        if absent:
            state["not_on_screen"] = absent
        state["objects"] = objs
        return state

    kept_rest = list(rest)
    kept_statics = dict(other_statics)
    kept_required = list(required)
    state = build(kept_required + kept_rest, kept_statics)
    while _size(state) > MAX_DECIDE_STATE_CHARS and kept_rest:
        kept_rest.pop()
        state = build(kept_required + kept_rest, kept_statics)
    while _size(state) > MAX_DECIDE_STATE_CHARS and kept_statics:
        kept_statics.pop(next(reversed(kept_statics)))
        state = build(kept_required + kept_rest, kept_statics)
    while _size(state) > MAX_DECIDE_STATE_CHARS and kept_required:
        kept_required.pop()
        state = build(kept_required + kept_rest, kept_statics)
    omitted = len(objects) - len(kept_required) - len(kept_rest)
    if omitted:
        state["omitted_objects"] = omitted
    sent_paths = {obj["path"] for obj in state["objects"]}
    live_paths = [obj["path"] for obj in objects]
    missing = sorted(
        name for name in names
        if any(_pinned({"path": path}, {name}) for path in live_paths)
        and not any(_pinned(obj, {name}) for obj in state["objects"])
    )
    missing += sorted(name for name in read_statics if name not in state.get("statics", {}))
    note = {
        "live": len(objects),
        "required": len(required),
        "sent": len(sent_paths),
        "omitted": omitted,
        "statics": len(state.get("statics", {})),
        "missing": missing,
    }
    return state, note


class JevStateFilter:
    """한 런의 executer 가 쓴다. 계획과 경로마다 Jev 의 답을 저장한다."""

    def __init__(self, jev: JevClient) -> None:
        self._jev = jev
        self._objects: dict[tuple[str, str], float] = {}
        self._members: dict[tuple[str, str, str], float] = {}

    async def objects(
        self, memories: MacroMemories, step: str, macros: StepMacros, options: dict[str, str], reads: Reads
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """`(거른 object, note)`. 고정한 것이 앞이고 확률 순이다. state 는 `assemble` 이 만든다.

        `reads` 가 고정의 근거다. 계획 source 만 보던 때는 developer macro 몸통이 읽는 object 가 고정되지 않았다
        (try 193 의 전투 turn 에서 고정 0, live 43 중 11 만 보냄).
        """
        scene = memories.scene_name
        objects = compact_objects(memories)
        plan = hashlib.sha1(macros.source.encode()).hexdigest()[:12]
        names = set(reads.objects)
        member_names = set(reads.members) | names
        context = {"scene": scene, "step": step, "actions": options}
        pins = {obj["path"] for obj in objects if _pinned(obj, names)}

        unasked = {
            bare(obj["path"]): obj
            for obj in objects
            if obj["path"] not in pins and (plan, bare(obj["path"])) not in self._objects
        }
        await self._ask(context, {
            (plan, path): (
                f"The object `{path}`"
                + (f" showing \"{str(obj.get('text'))[:80]}\"" if obj.get("text") else "")
                + f" with fields {json.dumps(obj.get('members') or {}, ensure_ascii=False, default=str)[:MAX_ASKED_MEMBERS_CHARS]}: "
                "does choosing the next action for this step, or checking whether its goal is reached, need this object?"
            )
            for path, obj in unasked.items()
        }, self._objects)

        scored = []
        for obj in objects:
            if obj["path"] in pins:
                scored.append((2.0, obj))
                continue
            probability = self._objects.get((plan, bare(obj["path"])), 1.0)
            if probability >= OBJECT_KEEP_AT:
                scored.append((probability, obj))
        if not scored:
            scored = [(1.0, obj) for obj in objects]
        scored.sort(key=lambda pair: -pair[0])
        ordered = [obj for _, obj in scored]

        unasked_members = {
            (plan, bare(obj["path"]), name): value
            for obj in ordered
            if len(obj["members"]) > MEMBER_HEAVY
            for name, value in obj["members"].items()
            if (plan, bare(obj["path"]), name) not in self._members
        }
        await self._ask(context, {
            key: (
                f"On the object `{key[1]}`, the field `{key[2]}` is "
                f"{json.dumps(value, ensure_ascii=False, default=str)[:MAX_ASKED_VALUE_CHARS]}: does choosing the next "
                "action for this step, or checking whether its goal is reached, need this field?"
            )
            for key, value in unasked_members.items()
        }, self._members)

        trimmed = []
        for obj in ordered:
            if len(obj["members"]) > MEMBER_HEAVY:
                obj = {**obj, "members": {
                    name: value for name, value in obj["members"].items()
                    if _member_pinned(name, member_names)
                    or self._members.get((plan, bare(obj["path"]), name), 1.0) >= MEMBER_KEEP_AT
                }}
            trimmed.append(obj)
        note = {
            "live": len(objects),
            "pinned": len(pins),
            "dropped": len(objects) - len(ordered),
        }
        return trimmed, note

    async def _ask(self, context: dict[str, Any], questions: dict[tuple, str], answers: dict) -> None:
        """묻지 않은 것만 `QUESTIONS_PER_REQUEST` 개씩 묻는다. 답하지 못한 것은 남긴다(1.0)."""
        items = list(questions.items())
        for start in range(0, len(items), QUESTIONS_PER_REQUEST):
            chunk = items[start:start + QUESTIONS_PER_REQUEST]
            try:
                got = await self._jev.probabilities(
                    {f"q{index}": text for index, (_key, text) in enumerate(chunk)},
                    context,
                    true_means=RELEVANT_MEANS,
                    false_means=IRRELEVANT_MEANS,
                )
            except JevError:
                got = {}
            for index, (key, _text) in enumerate(chunk):
                answers[key] = got.get(f"q{index}", 1.0)
