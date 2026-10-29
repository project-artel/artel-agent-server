"""한 런의 macro 초안과 등록된 정의.

`MacroBook` 을 tool 과 따로 재는 이유는 그것이 이 클래스가 있는 이유이기 때문이다 —
초안의 수명주기 규칙(쓰고·읽고·고치고·등록한다)이 tool closure 안에 있으면 tool 을
통해서만 재어진다.

그리고 `register` 는 **좁혀 둔 저장 자리**다. `artel-orchestration-server` 가
`content_map` 에 적는 frame 을 내놓으면 바뀌는 것이 그 메서드 하나이므로, 거기 붙은
규칙 — 제자리 갱신, 관계를 더하기만 하는 것 — 은 그것이 사는 자리에서 못박혀 있어야
한다.
"""

from app.agents.qa.macro.book import MacroBook
from app.agents.qa.macro.parser import macro_definition_from_source

FIRST = 'def deal(card: object) -> None:\n    click(card)\n'
SECOND = 'def deal(card: object) -> None:\n    double_click(card)\n'


def definition(source: str):
    return macro_definition_from_source("deal", source)


def test_a_fresh_book_holds_nothing() -> None:
    book = MacroBook()

    assert book.draft("deal") is None
    assert book.registered("deal") is None
    assert book.source("deal") is None
    assert book.was_read("deal") is False


def test_a_draft_is_what_source_returns_while_one_exists() -> None:
    """같은 이름의 초안이 있으면 초안을, 없으면 등록된 것의 원문을."""
    book = MacroBook()
    book.register(definition(FIRST), ())

    assert book.source("deal") == FIRST

    book.write("deal", SECOND)
    assert book.source("deal") == SECOND
    # 등록된 행은 건드리지 않았다.
    assert book.registered("deal").definition.source == FIRST


def test_writing_a_draft_replaces_it_outright() -> None:
    book = MacroBook()
    book.write("deal", FIRST)
    book.write("deal", SECOND)

    assert book.draft("deal") == SECOND


def test_registering_drops_the_draft_it_came_from() -> None:
    """남겨 두면 `read_macro` 가 등록된 것과 같은 글을 초안이라고 돌려준다.

    그러면 agent 는 그것을 아직 등록 안 된 것으로 읽는다.
    """
    book = MacroBook()
    book.write("deal", FIRST)
    book.register(definition(FIRST), ("Battle",))

    assert book.draft("deal") is None
    assert book.registered("deal").definition.source == FIRST


def test_registering_again_replaces_the_definition_in_place() -> None:
    book = MacroBook()
    book.register(definition(FIRST), ("Battle",))
    book.register(definition(SECOND), ())

    assert book.registered("deal").definition.source == SECOND
    # 지우고 새로 넣지 않는다. `ON DELETE CASCADE` 로 관계 행이 같이 사라진다.
    assert book.registered("deal").screens == ("Battle",)


def test_registering_adds_to_the_relations_and_never_takes_one_away() -> None:
    """관계를 빼는 길은 v1 에 없다. agent 가 공들여 단 것이라 잃으면 안 된다."""
    book = MacroBook()
    book.register(definition(FIRST), ("Battle",))
    book.register(definition(FIRST), ("Shop", "Map"))

    assert book.registered("deal").screens == ("Battle", "Shop", "Map")


def test_a_relation_is_not_recorded_twice_and_keeps_the_order_it_arrived_in() -> None:
    book = MacroBook()
    book.register(definition(FIRST), ("Battle", "Shop"))
    book.register(definition(FIRST), ("Shop", "Battle", "Map"))

    assert book.registered("deal").screens == ("Battle", "Shop", "Map")


def test_an_empty_relation_is_dropped_rather_than_recorded() -> None:
    """서 있는 `screen` 을 모르면 빈 문자열이 온다. 빈 관계를 적을 자리가 아니다."""
    book = MacroBook()
    book.register(definition(FIRST), ("", "Battle", ""))

    assert book.registered("deal").screens == ("Battle",)


def test_reading_is_remembered_per_name() -> None:
    book = MacroBook()
    book.write("deal", FIRST)

    assert book.was_read("deal") is False
    book.remember_read("deal")
    assert book.was_read("deal") is True
    assert book.was_read("something_else") is False
