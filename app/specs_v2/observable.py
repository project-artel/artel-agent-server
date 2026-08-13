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

# 전제가 통째로 답할 수 없는 것과, 일부만 답할 수 없는 것은 다르다. 앞엣것은
# 테스터가 어디서 시작할지조차 모르고, 뒤엣것은 읽을 수 있는 상태로 자리를 잡은
# 뒤 나머지를 화면으로 가늠할 수 있다.
UNCHECKABLE = "precondition_not_observable"
PARTLY = "precondition_partly_observable"


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


