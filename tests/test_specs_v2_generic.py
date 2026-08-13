"""판정이 이 게임의 이름을 하나도 모르는지.

한 프로젝트의 식별자로 규칙을 만들면 다음 프로젝트에서 아무것도 못 찾고, **못
찾았다는 사실조차** 찾을 것이 없었다는 말과 구별되지 않는다. 그래서 여기 있는
검사는 전부 처음 보는 이름으로 쓴다.
"""

import re

from app.specs_v2 import observable
from app.specs_v2.answers import Answers


def test_no_rule_is_written_from_a_projects_identifiers() -> None:
    """규칙 안에 게임에서 온 낱말이 있으면 그 규칙은 그 게임에서만 맞다."""
    source = observable.__file__.replace(".py", ".py")
    with open(source, encoding="utf-8") as handle:
        code = "\n".join(
            line for line in handle if not line.lstrip().startswith("#")
        )
    # 표본 게임의 이름들. 여기 있는 어느 것도 규칙이 알아서는 안 된다.
    for name in (
        "bigSide",
        "distanceToPlayer",
        "streamingCoroutine",
        "StagePosition",
        "chatText",
        "anyKeyPrompt",
        "MapMove",
        "StoryController",
    ):
        assert not re.search(rf'["\'(|]{name}["\')|]', code), (
            f"{name} 은 이 게임의 이름이다"
        )


def test_a_name_with_no_owner_is_something_on_the_stack() -> None:
    """필드는 언제나 주인을 달고 온다. 맨 이름은 프레임이 끝나면 없다."""
    # 대소문자를 안 본다: "지역 변수는 소문자" 는 관례이지 구조가 아니다.
    for unowned in ("i", "tick", "howManyLeft", "Temp", "N"):
        assert not observable.readable(unowned)
    for owned in ("Foo.bar", "Some.Deep.chain"):
        assert observable.readable(owned)


def test_a_literal_is_not_mistaken_for_a_local() -> None:
    """`null` 과 `3` 은 주인이 없지만 스택에 있지도 않다."""
    for literal in ("null", "true", "false", "3", "-1", "1.5"):
        leaf = {"kind": "test", "left": "Foo.bar", "operator": "==", "right": literal}
        assert observable.unreadable_atoms(leaf) == []


def test_an_argument_is_caught_even_when_it_wears_an_owner() -> None:
    """`something.Method(...)` 는 필드 사슬처럼 보인다. SDK 가 인자라고 말해 준다."""
    disguised = {
        "kind": "test",
        "left": 'incoming.SomeCheck("x")',
        "operator": "!=",
        "right": "0",
        "context": "arg:0",
    }
    assert observable.unreadable_atoms(disguised)

    owned = {**disguised, "left": "Holder.member", "context": "this"}
    assert observable.unreadable_atoms(owned) == []


def test_a_bare_name_is_qualified_and_a_literal_is_not() -> None:
    condition = {
        "kind": "every",
        "parts": [
            {"kind": "test", "left": "counter", "operator": "<", "right": "9"},
            {"kind": "test", "left": "Holder.member", "operator": "==", "right": "null"},
        ],
    }
    qualified = observable.qualify(condition, "Where.It.Lives")

    assert qualified["parts"][0]["left"] == "Where.It.Lives.counter"
    assert qualified["parts"][0]["right"] == "9"
    assert qualified["parts"][1] == condition["parts"][1]


def test_a_reference_is_recognised_by_what_the_evidence_assigns_it() -> None:
    """이름이 무엇이든, 근거가 `null` 을 넣은 적 있으면 참조다."""

    class Path:
        source_signature = "System.Void Any.Type::Any()"
        call_path = ("System.Void Any.Type::Any()",)
        calls: list = []
        condition = {"kind": "always"}
        effects = [
            {"kind": "write", "target": "Any.handle", "detail": "null", "offset": 1},
            {"kind": "write", "target": "Any.count", "detail": "0", "offset": 2},
        ]

    known = Answers.of([Path()])

    assert known.references == {"Any.handle"}
    assert (
        known.resolve(
            {"kind": "test", "left": "Any.handle", "operator": "!=", "right": "0"}, set()
        )[0]["right"]
        == "null"
    )
    assert (
        known.resolve(
            {"kind": "test", "left": "Any.count", "operator": "!=", "right": "0"}, set()
        )[0]["right"]
        == "0"
    )
