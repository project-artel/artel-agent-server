"""Where the evidence keeps the answer to what it could not say in place.

The SDK marks the spots it cannot read rather than guessing at them, and every
mark has an answer one hop away in the same report. There are only three hops —
`callPath`, `calls`, `effects` — so there are only three kinds of answer, and
they are gathered in one pass and applied in one walk.

    a parameter        `(not a literal)`      → what the call site passed
    a return value     `LoadPlayData()`       → the one field the method wrote
    an unreadable guard `i >= …Length`        → what its own branch assigned

The first two are provenance: the value came from somewhere and the somewhere is
named. The third is not — it asks what the branch leaves behind rather than
where a value came from — but it is the same shape of move, and keeping it here
is what makes the shape visible. A premise nobody can read has an answer or it
does not, and this module is the whole of where to look.

Nothing here reads what the code means. Every answer is a field name or a
literal already written in the evidence, joined to the question by the path the
SDK drew.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import observable

# A call with no arguments, at the end of a chain. Arguments would make the
# answer depend on them, and the field a method writes is only the same answer
# when the call is the same call.
NILADIC = re.compile(r"^(?P<head>.*?)(?:^|\.)(?P<method>[A-Za-z_]\w*)\(\s*\)$")
# 시그니처가 반환 타입을 앞에 적어 둔다. `get_` 이 붙으면 프로퍼티, 없으면 메서드다.
BOOLEAN_CALL = re.compile(r"^System\.Boolean .+::(?:get_)?(?P<name>[A-Za-z_]\w*)\(")
LITERAL_ARG = re.compile(r'^(?:true|false|-?\d+(?:\.\d+)?|"[^"]*")$', re.IGNORECASE)

# How the SDK writes a value it could not read in place. Each is a parameter of
# the method the effect sits in, so the caller is the one that knows.
PARAMETER = frozenset({"(not a literal)", "_", "(not a simple target)"})

# Recorded on the row so a reader can see the premise is not what the code says.
# Not defects: each means the row became answerable.
SUBSTITUTED = "precondition_read_from_written_field"
RESTATED = "precondition_rewritten_to_observable"
NOTES = frozenset({SUBSTITUTED, RESTATED})


def method_of(signature: Any) -> str:
    return str(signature or "").split("::")[-1].split("(")[0]


@dataclass
class Answers:
    """One lookup, gathered once per report."""

    # (caller, callee) → what that call site passes, when it passes one thing a
    # reader can be asked for: a literal, or a field. Keyed by the pair
    # because a callee is called both ways: `SetAnyKeyPromptVisible(true)` when
    # the typing finishes and `(false)` when the next line starts. There is no
    # answer for the method and there is one for each route to it.
    passed: dict[tuple[str, str], str] = field(default_factory=dict)
    # method → the one field it writes. `LoadPlayData()` answers with the saved
    # progress and writes the same value into `MapMove.StagePosition` on the way
    # out, so after it has run the field holds what it answered.
    wrote: dict[str, str] = field(default_factory=dict)
    # unreadable guard → the equalities its own branch establishes. An assignment
    # is an equality once it has run.
    left_behind: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # Fields the evidence assigns `null`. IL spells a null check as a comparison
    # against zero, so the SDK reports `streamingCoroutine != 0` — faithfully,
    # since it has no type to say otherwise. The runtime reader publishes that
    # field as `null` or as what it points at, and `0` is a value it will never
    # show. Somewhere else in the same evidence the field is written `null`, and
    # that is what says it holds a reference.
    references: set[str] = field(default_factory=set)
    # 참/거짓을 담는 이름. IL 은 `if (flag)` 를 정수 비교로 낮추므로 SDK 에는 `!= 0`
    # 으로 도착한다. 참조의 null 검사도 같은 명령이라 **둘이 한 모양으로 온다** —
    # 무엇이었는지는 시그니처의 반환 타입이 말한다.
    booleans: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, paths: Any) -> "Answers":
        passed: dict[tuple[str, str], set[str]] = {}
        wrote: dict[str, list[str]] = {}
        left: dict[str, list[tuple[str, str]]] = {}
        references: set[str] = set()
        booleans: set[str] = set()

        for path in paths:
            here = method_of(path.source_signature)

            for call in path.calls:
                found = BOOLEAN_CALL.match(str(call.get("target") or ""))
                if found:
                    booleans.add(found.group("name"))
                argument = str(call.get("args") or "").strip()
                callee = method_of(call.get("target"))
                if not (here and callee) or "," in argument:
                    # More than one argument and which parameter a premise names
                    # is not something the evidence says. It gives the arguments
                    # as one string and the parameter by the name the method
                    # gave it, and nothing joins the two.
                    continue
                if LITERAL_ARG.match(argument):
                    passed.setdefault((here, callee), set()).add(argument.lower())
                elif observable.FIELD_CHAIN.match(argument):
                    # A field reaches the callee as a parameter, and inside the
                    # callee the premise names the parameter — unreadable, while
                    # the field it came from is not.
                    passed.setdefault((here, callee), set()).add(argument)

            equalities = [pair for pair in map(_equality, path.effects) if pair]
            for effect in path.effects:
                if effect.get("kind") not in {"write", "saved"}:
                    continue
                target = str(effect.get("target") or "").strip()
                if here and observable.FIELD_CHAIN.match(target):
                    if target not in wrote.setdefault(here, []):
                        wrote[here].append(target)
                if str(effect.get("detail") or "").strip() == "null":
                    references.add(target)
                if str(effect.get("detail") or "").strip().lower() in {"true", "false"}:
                    booleans.add(target.rsplit(".", 1)[-1])

            if equalities:
                for leaf in observable._leaves(path.condition):
                    text = observable._leaf_text(leaf)
                    if text is None or observable._leaf_readable(leaf):
                        continue
                    kept = left.setdefault(text, [])
                    for pair in equalities:
                        if pair not in kept:
                            kept.append(pair)

        return cls(
            # Only where a caller is consistent, and only where a method writes
            # exactly one field. Two of either and which one answers has no
            # structural answer; guessing would put a premise in the sheet that
            # the evidence does not support.
            passed={pair: next(iter(seen)) for pair, seen in passed.items() if len(seen) == 1},
            wrote={name: found[0] for name, found in wrote.items() if len(found) == 1},
            left_behind=left,
            references=references,
            booleans=booleans,
        )

    def parameter_value(self, path: Any) -> str | None:
        """이 경로로 왔을 때 매개변수가 받은 리터럴.

        효과가 값을 `(not a literal)` 이라고 적어 두는 자리에 쓴다. 메서드 안에서는
        리터럴이 아닌 것이 맞고, 그것을 그렇게 만든 호출부는 바로 앞 홉이다.
        """
        if len(path.call_path) < 2:
            return None
        answer = self.passed.get(
            (method_of(path.call_path[-2]), method_of(path.source_signature))
        )
        return answer if answer and LITERAL_ARG.match(answer) else None

    def _binding(
        self, condition: dict[str, Any] | None, route: tuple[str, ...]
    ) -> tuple[str, str] | None:
        """(parameter name, what the call site passed), when both are unambiguous."""
        if len(route) < 2:
            return None
        answer = self.passed.get((method_of(route[-2]), method_of(route[-1])))
        if not answer or LITERAL_ARG.match(answer):
            return None
        name = self.sole_parameter(condition)
        return (name, answer) if name else None

    def sole_parameter(self, condition: dict[str, Any] | None) -> str | None:
        """The one unreadable bare name a method's premises use, if there is one.

        With a single argument and a single such name the binding is not a
        guess: there is nothing else the argument could have become.
        """
        names = {
            _bare_of(leaf.get(side))
            for leaf in observable._leaves(condition)
            for side in ("left", "right")
            if side in (leaf.get("localFrames") or {})
        }
        return names.pop() if len(names) == 1 else None

    def zero_as_written(self, node: dict[str, Any]) -> dict[str, Any]:
        """IL 이 `0` 으로 낮춘 것을 원래 쓰여 있던 대로 되읽는다.

        `if (flag)` 도 `if (handle != null)` 도 같은 명령이 되어, SDK 에는 둘 다
        `!= 0` 으로 도착한다. 근거가 무엇이었는지는 따로 말해 준다 — 참조는 어딘가에서
        `null` 을 대입받고, 참/거짓은 시그니처가 `System.Boolean` 을 앞에 적는다.

        `0` 으로 두면 판독기가 내보내는 `null` 이나 `true`/`false` 와 글자가 맞지 않아,
        사람이 무엇과 비교해야 하는지 알 수 없다. 조건에서 온 항이든 대입에서 세운
        등식이든 같은 낮춤을 거쳤으므로 한 자리에서 읽는다.
        """
        if str(node.get("right") or "").strip() != "0":
            return node
        left = str(node.get("left") or "").strip()
        # 같은 필드가 주인을 달리 적어 온다 — 자기 타입으로 한 번, 그것을 들고 있는
        # 쪽에서 한 번. 한 조건 안에 `A.handle == null` 과 `B.thing.handle != 0` 이
        # 나란히 서면 같은 것을 두 값으로 재는 문장이 된다. 마지막 마디로 맞춘다.
        if left in self.references or _tail(left) in {
            _tail(name) for name in self.references
        }:
            return {**node, "right": "null", "nullComparison": True}
        if _tail(left) in self.booleans:
            return {
                **node,
                "right": "true" if node.get("operator") == "!=" else "false",
                "operator": "==",
                "booleanComparison": True,
            }
        return node

    def resolve(
        self,
        condition: dict[str, Any] | None,
        asserted: set[str],
        route: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Say each premise through whatever the evidence can answer it with.

        A call first: the field it wrote is the same answer, and saying the
        premise that way leaves nothing for the branch equality to do.

        `asserted` is what this row already expects. An answer naming one of
        those is refused — a premise and an expectation that are the same
        observation make a row that cannot fail, which is the failure this
        module exists to prevent, and it must not come back through the
        mechanism meant to stop it. Compared by subject rather than by text: the
        expectation holds a resolved scene path and the answer holds the code's
        own chain, and those never match as strings.
        """
        subjects = {observable.subject(item) for item in asserted}
        notes: list[str] = []

        # A parameter is unreadable inside the method and the call site says what
        # became it. Done first and by name, because after `qualify` the premise
        # spells the parameter with its frame in front and no longer looks bare.
        bound = self._binding(condition, route)

        def walk(node: dict[str, Any]) -> dict[str, Any]:
            if node.get("kind") in {"every", "either"}:
                return {**node, "parts": [walk(part) for part in node.get("parts") or ()]}
            if node.get("kind") != "test":
                return node

            changed = dict(node)
            if bound:
                for side in ("left", "right"):
                    frames = changed.get("localFrames") or {}
                    if side in frames and str(changed[side]).endswith(f".{bound[0]}"):
                        changed[side] = bound[1]
                        changed.setdefault("boundArguments", {})[side] = bound[0]
                        # The marks that said "this is a parameter" are spent
                        # once the parameter has been replaced by what became it.
                        # Left in place they keep the premise unreadable after it
                        # has stopped being one.
                        changed = {
                            key: value
                            for key, value in changed.items()
                            if key not in {"localFrames", "context"}
                        }
                        notes.append(SUBSTITUTED)
            changed = self.zero_as_written(changed)
            for side in ("left", "right"):
                text = str(changed.get(side) or "").strip()
                found = NILADIC.match(text)
                if not found:
                    continue
                answer = self.wrote.get(found.group("method"))
                if not answer:
                    continue
                changed[side] = answer
                changed.setdefault("substitutedCalls", {})[side] = text
                notes.append(SUBSTITUTED)
            if changed is not node and observable._leaf_readable(changed):
                return changed

            text = observable._leaf_text(changed)
            if text is None or observable._leaf_readable(changed):
                return changed
            for target, value in self.left_behind.get(text, ()):
                if observable.subject(target) in subjects:
                    continue
                notes.append(RESTATED)
                # 효과의 값도 같은 낮춤을 거쳐 온다. `SetLocked(false)` 는 `0` 으로
                # 적히므로, 대입에서 세운 등식도 같은 자리에서 읽어야 한다.
                return self.zero_as_written({
                    "kind": "test",
                    "left": target,
                    "operator": "==",
                    "right": value,
                    "context": changed.get("context"),
                    "offset": changed.get("offset"),
                    # Kept so a reader can see this is not what the code says.
                    # The code says a counter reached a length; this says what
                    # that leaves behind.
                    "observableProxyFor": text,
                })
            return changed

        if not condition:
            return condition, notes
        return walk(condition), sorted(set(notes))


def negates_own_guard(condition: dict[str, Any] | None, effects: Any) -> bool:
    """효과가 자기 가드를 거짓으로 만드는가.

    `CompleteStream` 은 `streamingCoroutine != null` 일 때 돌고, 도는 동안 그것을
    `null` 로 만든다. 그러면 가드와 효과가 **동시에 참인 순간이 없다** — 하나는
    이전이고 하나는 이후다.

    그런 레코드는 상태가 아니라 **전이**다. "이 상태로 진입해 관찰하면 저것이
    보인다" 로 쓰면 어느 순간에도 참이 아닌 문장이 되고, 실제로 스트리밍 중에
    판독하면 본문은 아직 부분이다.
    """
    if not condition:
        return False
    written = {
        str(effect.get("target") or "").strip(): observable.value_of(effect.get("detail"))
        for effect in effects
    }
    for leaf in observable._leaves(condition):
        if leaf.get("kind") != "test":
            continue
        left = str(leaf.get("left") or "").strip()
        if left not in written:
            continue
        right = str(leaf.get("right") or "").strip()
        after = written[left]
        operator = str(leaf.get("operator") or "")
        if operator == "!=" and after == right:
            return True
        if operator == "==" and after and after != right:
            return True
    return False


def _tail(name: Any) -> str:
    """이름의 마지막 마디. `A.b.Flag` 도 `A.Flag()` 도 `Flag` 로 읽는다."""
    return str(name or "").strip().rstrip(")").rstrip("(").rsplit(".", 1)[-1]


def _bare_of(name: Any) -> str:
    return str(name or "").rsplit(".", 1)[-1]


def _equality(effect: dict[str, Any]) -> tuple[str, str] | None:
    """The equality an assignment leaves behind, when both sides are readable.

    Field to field only. `streamingText = String.Concat(_, " ")` assigns from a
    masked parameter and says nothing anyone can check, so it answers nothing.
    """
    if effect.get("kind") not in {"ui-value", "write", "active-state"}:
        return None
    target = str(effect.get("target") or "").strip()
    value = observable.value_of(effect.get("detail"))
    if not observable.FIELD_CHAIN.match(target):
        return None
    if not (observable.FIELD_CHAIN.match(value) or value in observable.EMPTY):
        return None
    return target, value
