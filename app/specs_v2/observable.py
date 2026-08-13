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
from typing import Any, Iterable

# `Type.member` or a longer chain of them. The runtime reader walks fields by
# reflection and publishes each one, so a chain of field names is a thing a
# reader can be asked for. A bare lowercase name is a local or a parameter.
FIELD_CHAIN = re.compile(r"^[A-Z]\w*(?:\.[A-Za-z_]\w*)+$")

# A name with no owner. Everything the reader can be asked for arrives owned —
# the SDK writes a field as `Type.member` — so a bare identifier is something
# that lives on the stack, and no widening of the reader reaches a frame that
# has ended.
#
# Matched on shape rather than on a list of names. A list is a list of *this*
# game's identifiers: `bigSide` and `distanceToPlayer` say nothing about the
# next project, and a rule built from them would find no locals there at all
# while reporting that it found none because there were none.
# 대소문자를 보지 않는다. "지역 변수는 소문자" 는 사람의 관례이지 구조가 아니고,
# 관례를 어긴 이름 하나가 조용히 필드로 읽힌다. 주인이 있느냐만 본다.
UNOWNED = re.compile(r"^\w+$")

# How the SDK says a leaf reads a parameter. Carries the position, so it also
# marks the ones that do not look bare: `collision.CompareTag("Enemy")` has an
# owner and the owner is an argument.
ARGUMENT = "arg:"

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


# 뺐다는 표시. 살림을 뺀 것은 결함이 아니라 **덜어낸 것**이다.
STACK_LOCAL = "premise_on_a_local_dropped"
# 가지를 가르던 항을 뺐다는 표시. 이쪽은 결함이다 — 행이 코드보다 넓은 말을 한다.
BRANCH_LOCAL = "branch_premise_not_observable"

# 서로를 부정하는 비교. 같은 두 항을 놓고 이것이 둘 다 나타나면 그 항은 가지를 가른다.
NEGATION = {"<": ">=", ">=": "<", ">": "<=", "<=": ">", "==": "!=", "!=": "=="}


def branch_selectors(
    conditions: Iterable[dict[str, Any] | None],
) -> set[tuple[str, str]]:
    """가지를 가르는 지역 항. 같은 두 항을 서로 부정하는 비교로 재는 자리다.

    `i < 총개수` 만 있으면 루프가 자기 진행을 재는 살림이다. 같은 자리에 `i >= 총개수`
    도 있으면 그것은 살림이 아니라 **갈림길**이다 — 한쪽은 다음 대사를 내고 다른 쪽은
    화면을 넘긴다. 두 행이 다른 일을 말하게 만드는 것이 바로 그 항이므로, 빼면 두 행이
    같은 전제 아래 다른 결과를 주장하게 된다.

    "마지막 대사에서 누르면 맵으로 간다" 가 "아무 때나 누르면 맵으로 간다" 가 되는 것을
    막는 자리다. 읽을 수 없다는 사실은 그대로 남고, 읽을 수 없다고 지우지는 않는다.
    """
    ways: dict[tuple[str, str], set[str]] = {}
    for condition in conditions:
        for leaf in _leaves(condition):
            if leaf.get("kind") != "test" or not leaf.get("localFrames"):
                continue
            pair = (str(leaf.get("left")), str(leaf.get("right")))
            ways.setdefault(pair, set()).add(str(leaf.get("operator")))
    return {
        pair
        for pair, operators in ways.items()
        if any(NEGATION.get(operator) in operators for operator in operators)
    }


def drop_locals(
    condition: dict[str, Any] | None, selectors: set[tuple[str, str]] = frozenset()
) -> tuple[dict[str, Any] | None, bool, bool]:
    """스택에 사는 값을 사전 조건에서 뺀다. 가지를 가르던 것은 뺀 사실을 남긴다.

    지역 변수는 테스터가 만들 수 있는 상태가 아니다. 값을 정해 줄 자리가 없고, 실행
    중인 게임에 물어볼 수도 없다 — 판독기는 필드를 읽지 스택 프레임을 읽지 않는다.
    남겨 두면 사람이 지울 수도 고칠 수도 없는 한 줄 때문에, 같은 조건의 읽을 수 있는
    나머지 — 어느 화면인지, 어떤 필드가 무엇인지 — 까지 함께 묻힌다.

    그렇다고 뺀 자리가 다 같지는 않다. `branch_selectors` 가 짚는 항은 두 행이 서로
    다른 일을 말하게 만드는 것이고, 그것 없이는 "마지막 대사에서 누르면 맵으로 간다"
    가 "아무 때나 누르면 맵으로 간다" 가 된다. 좁은 참이 넓은 거짓이 되는 자리다.

    남겨 두는 것으로는 풀리지 않는다. 같은 카운터를 루프 진입 시점과 `i + 1` 이후
    시점에서 읽은 두 항이 한 조건에 들어가면 동시에 참일 수 없고, 모순으로 판정되어
    행이 통째로 사라진다. 그래서 빼되 **뺐다는 사실을 등급에 남긴다** — 행은 남고,
    사람이 조건 하나가 빠진 채로 읽고 있다는 것을 안다.
    """
    dropped = False
    narrowing = False

    def walk(node: dict[str, Any]) -> dict[str, Any] | None:
        nonlocal dropped, narrowing
        if node.get("kind") in {"every", "either"}:
            kept = [
                part
                for part in (walk(item) for item in node.get("parts") or ())
                if part is not None
            ]
            if not kept:
                return None
            return kept[0] if len(kept) == 1 else {**node, "parts": kept}
        if node.get("kind") == "test" and node.get("localFrames"):
            if (str(node.get("left")), str(node.get("right"))) in selectors:
                narrowing = True
            dropped = True
            return None
        return node

    if not condition:
        return condition, False, False
    return (walk(condition) or {"kind": "always"}), dropped, narrowing


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
        value = str(changed.get(side) or "").strip()
        if UNOWNED.match(value) and not _literal(value):
            changed[side] = f"{frame}.{value}"
            changed.setdefault("localFrames", {})[side] = frame
    return changed


def readable(expression: Any) -> bool:
    """Whether the runtime reader can be asked for this."""
    text = str(expression or "").strip()
    if not text:
        return False
    if UNOWNED.match(text) and not _literal(text):
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
    # The SDK names the frame for us when a leaf reads a parameter, and that is
    # the only signal for the ones an owner hides — `collision.CompareTag(…)`
    # looks like a field chain and is an argument.
    if str(leaf.get("context") or "").startswith(ARGUMENT):
        return False
    return readable(leaf.get("left")) and (
        readable(leaf.get("right")) or str(leaf.get("right") or "") in EMPTY or _literal(leaf.get("right"))
    )


def _literal(value: Any) -> bool:
    """스택이 아니라 코드 안에 그대로 적힌 값."""
    text = str(value or "").strip()
    if text.lower() in {"null", "true", "false"}:
        return True
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


