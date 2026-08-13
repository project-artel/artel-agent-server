"""Rewriting a premise nobody can read into one the running game reports.

A premise a tester cannot check is not a premise. `i >= streamingText.Length`
names a loop counter that exists only while its method runs, so the only way to
confirm it is to look at what it caused — and if that is the same thing the row
already expects, the row can never fail.

The rewrite needs no understanding of what the code means. An assignment is an
equality once it has run: after `chatText.text = streamingText`, the statement
`chatText.text == streamingText` is true, and both sides are fields the runtime
reader publishes. So a branch guarded by something unreadable, whose body
assigns one readable field from another, has an observable form — and it is
written in the evidence already, as `effects[].target` and `effects[].detail`.

Two limits keep this honest.

The equality has to be *field to field*. `streamingText = String.Concat(_, " ")`
assigns from a masked parameter and says nothing anyone can check, so it is not
a proxy for anything.

And the proxy must not restate what the row expects. Substituting
`chatText.text == streamingText` into the premise of the very row asserting that
assignment produces a test whose premise and expectation are the same
observation — the failure this module exists to prevent, reintroduced by its own
mechanism.
"""

from __future__ import annotations

import re
from typing import Any

# `Type.member` or a longer chain of them. The runtime reader walks fields by
# reflection and publishes each one, so a chain of field names is a thing a
# reader can be asked for. A bare lowercase name is a local or a parameter.
FIELD_CHAIN = re.compile(r"^[A-Z]\w*(?:\.[A-Za-z_]\w*)+$")

# Names the compiler gave, not the game. Read on their own they are loop
# counters and parameters, and no widening of the reader reaches a frame that
# has ended.
BARE = re.compile(r"^(?:i|j|k|n|id|num|index|damage|collision|other|bigSide|distanceToPlayer)$")

# What a reference reads as when nothing was assigned.
EMPTY = frozenset({"null", "0", "false", "true"})

ISSUE = "precondition_rewritten_to_observable"
# 전제가 통째로 답할 수 없는 것과, 일부만 답할 수 없는 것은 다르다. 앞엣것은
# 테스터가 어디서 시작할지조차 모르고, 뒤엣것은 읽을 수 있는 상태로 자리를 잡은
# 뒤 나머지를 화면으로 가늠할 수 있다.
UNCHECKABLE = "precondition_not_observable"
PARTLY = "precondition_partly_observable"
SUBSTITUTED = "precondition_read_from_written_field"


def unreadable_atoms(condition: dict[str, Any] | None) -> list[str]:
    """The premises left that nothing reading the running game can answer.

    Called after the rewrite, so what remains is what no branch could restate: a
    counter that lives on the stack, a call the reader must not make. A row
    carrying one is not runnable as written — the tester has no way to establish
    it and no way to confirm it was established.
    """
    return [
        text
        for leaf in _leaves(condition)
        if (text := _leaf_text(leaf)) is not None and not _leaf_readable(leaf)
    ]


def readable_atoms(condition: dict[str, Any] | None) -> list[str]:
    """The premises the running game does answer."""
    return [
        text
        for leaf in _leaves(condition)
        if (text := _leaf_text(leaf)) is not None and _leaf_readable(leaf)
    ]


def qualify(condition: dict[str, Any] | None, frame: str) -> dict[str, Any] | None:
    """Name each bare local for the method it lives in.

    `i` is the dialogue loop's line index in one method and the typing
    animation's character index in another, and both arrive spelled `i` with
    `context: this`. Composed into one condition they read as one variable, and
    `i < lineCount 그리고 i >= textLength` is a sentence about nothing.

    Qualifying before composition keeps them apart. It does not make either
    readable — see `unreadable_atoms` — but a premise nobody can check is a
    smaller problem than a premise that was never in the code.
    """
    if not condition:
        return condition
    if condition.get("kind") in {"every", "either"}:
        return {
            **condition,
            "parts": [qualify(part, frame) for part in condition.get("parts") or ()],
        }
    if condition.get("kind") != "test":
        return condition
    changed = dict(condition)
    for side in ("left", "right"):
        value = str(changed.get(side) or "")
        if BARE.match(value):
            changed[side] = f"{frame}.{value}"
            changed.setdefault("localFrames", {})[side] = frame
    return changed


def readable(expression: Any) -> bool:
    """Whether the runtime reader can be asked for this."""
    text = str(expression or "").strip()
    if not text:
        return False
    if BARE.match(text):
        return False
    # A call is not made. Reading the game must not change it, and several of
    # these have side effects.
    if "(" in text:
        return False
    return True


def value_of(detail: Any) -> str:
    """The value an assignment carries, without the API's own second argument.

    `SetText(value, syncTextInputBox)` reaches the evidence as
    `streamingText, true`, and the flag is not part of what anyone checks. Read
    in one place because two readers of the same field disagreeing is how
    `_, true` reached a test step: the rewrite dropped the flag and saw a masked
    parameter, the sentence kept it and saw a value.
    """
    value = str(detail or "").strip()
    if value.endswith(", true") or value.endswith(", false"):
        value = value.rsplit(",", 1)[0].strip()
    return value


def _equality(effect: dict[str, Any]) -> tuple[str, str] | None:
    """The equality an assignment leaves behind, when both sides are readable."""
    if effect.get("kind") not in {"ui-value", "write", "active-state"}:
        return None
    target = str(effect.get("target") or "").strip()
    value = value_of(effect.get("detail"))
    if not FIELD_CHAIN.match(target):
        return None
    if not (FIELD_CHAIN.match(value) or value in EMPTY):
        return None
    return target, value


# `Owner.field.Method()` — a call with no arguments, at the end of a chain.
# Arguments would make the answer depend on them, and the field a method writes
# is only the same answer when the call is the same call.
NILADIC = re.compile(r"^(?P<head>.*?)(?:^|\.)(?P<method>[A-Za-z_]\w*)\(\s*\)$")


def written_fields(paths: Any) -> dict[str, str]:
    """Method name → the one field it writes, when it writes exactly one.

    `SaveLoadController.LoadPlayData()` returns the saved progress and writes the
    same value into `MapMove.StagePosition` on the way out. Nothing may call it —
    reading the game must not change it, and this one saves — but after it has
    run, the field holds what it answered.

    Only when there is exactly one write. Two and the question of which one
    answers the call has no structural answer, and guessing it would put a
    premise in the sheet that the evidence does not support.
    """
    writes: dict[str, list[str]] = {}
    for path in paths:
        method = str(path.source_signature or "").split("::")[-1].split("(")[0]
        if not method:
            continue
        for effect in path.effects:
            if effect.get("kind") not in {"write", "saved"}:
                continue
            target = str(effect.get("target") or "").strip()
            if FIELD_CHAIN.match(target) and target not in writes.setdefault(method, []):
                writes[method].append(target)
    return {method: found[0] for method, found in writes.items() if len(found) == 1}


def substitute_calls(
    condition: dict[str, Any] | None, written: dict[str, str]
) -> tuple[dict[str, Any] | None, list[str]]:
    """Say a premise through the field its call left behind."""
    swapped: list[str] = []

    def walk(node: dict[str, Any]) -> dict[str, Any]:
        if node.get("kind") in {"every", "either"}:
            return {**node, "parts": [walk(part) for part in node.get("parts") or ()]}
        if node.get("kind") != "test":
            return node
        changed = dict(node)
        for side in ("left", "right"):
            text = str(changed.get(side) or "").strip()
            found = NILADIC.match(text)
            if not found:
                continue
            field = written.get(found.group("method"))
            if not field:
                continue
            swapped.append(text)
            changed[side] = field
            changed.setdefault("substitutedCalls", {})[side] = text
        return changed

    if not condition:
        return condition, swapped
    return walk(condition), swapped


def proxies(paths: Any) -> dict[str, list[tuple[str, str]]]:
    """Unreadable guard text → the equalities its own branch establishes.

    Built over every path rather than per contract because a guard travels: the
    condition that stops the typing animation is composed into rows written
    about what happens after it, and by then the assignment that answers it is
    several hops away.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    for path in paths:
        equalities = [pair for pair in map(_equality, path.effects) if pair]
        if not equalities:
            continue
        for leaf in _leaves(path.condition):
            text = _leaf_text(leaf)
            if text is None or _leaf_readable(leaf):
                continue
            kept = found.setdefault(text, [])
            for pair in equalities:
                if pair not in kept:
                    kept.append(pair)
    return found


def _leaves(condition: dict[str, Any] | None):
    if not condition:
        return
    if condition.get("kind") in {"every", "either"}:
        for part in condition.get("parts") or ():
            yield from _leaves(part)
        return
    yield condition


def _leaf_text(leaf: dict[str, Any]) -> str | None:
    if leaf.get("kind") != "test":
        return None
    return f"{leaf.get('left')} {leaf.get('operator')} {leaf.get('right')}"


def _leaf_readable(leaf: dict[str, Any]) -> bool:
    # A qualified local reads as a field chain — `Story/<Tell>d__1.MoveNext.i`
    # has the shape of one — so the mark `qualify` left is what says otherwise.
    # Without it the naming that keeps two counters apart would also hide that
    # neither can be read.
    if leaf.get("localFrames"):
        return False
    return readable(leaf.get("left")) and (
        readable(leaf.get("right")) or str(leaf.get("right") or "") in EMPTY or _literal(leaf.get("right"))
    )


def _literal(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(re.match(r'^-?\d+(\.\d+)?$|^".*"$', text))


# Value accessors: naming one of these still names the object in front of it.
# `ChatWindowController.chatText.text` and the resolved `Canvas/ChatWindow.chatText`
# are the same subject written two ways, and the row's own expectation is stored
# in the resolved form.
ACCESSOR = frozenset({"text", "sprite", "value", "activeSelf", "gameObject", "transform", "name"})


def subject(target: str) -> str:
    """The object a target names, with the value accessor and the owner dropped."""
    parts = [part for part in str(target or "").split(".") if part]
    while len(parts) > 1 and parts[-1] in ACCESSOR:
        parts.pop()
    return parts[-1] if parts else ""


def rewrite(
    condition: dict[str, Any],
    table: dict[str, list[tuple[str, str]]],
    asserted: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """Replace unreadable leaves with the equality their branch establishes.

    `asserted` is what this row already expects. A proxy naming one of those is
    dropped rather than substituted — see the module docstring. Compared by
    subject rather than by text: the expectation holds a resolved scene path and
    the proxy holds the code's own chain, and those never match as strings.
    """
    subjects = {subject(item) for item in asserted}
    swapped: list[str] = []

    def walk(node: dict[str, Any]) -> dict[str, Any]:
        if node.get("kind") in {"every", "either"}:
            return {**node, "parts": [walk(part) for part in node.get("parts") or ()]}
        text = _leaf_text(node)
        if text is None or _leaf_readable(node):
            return node
        for target, value in table.get(text, ()):
            if subject(target) in subjects:
                continue
            swapped.append(text)
            return {
                "kind": "test",
                "left": target,
                "operator": "==",
                "right": value,
                "context": node.get("context"),
                "offset": node.get("offset"),
                # Kept so a reader can see this is not what the code says. The
                # code says a counter reached a length; this says what that
                # leaves behind.
                "observableProxyFor": text,
            }
        return node

    return walk(condition), swapped
