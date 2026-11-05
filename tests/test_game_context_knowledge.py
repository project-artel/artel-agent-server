from app.agents.game_context.schemas import (
    Entity,
    Flow,
    GameContext,
    GlossaryItem,
    Mechanic,
    MiscItem,
    Overview,
    ProgressionItem,
    Screen,
)
from app.agents.qa.knowledge import KNOWLEDGE_TAGS
from app.api.game_context_knowledge import game_context_to_knowledge_items


def test_empty_game_context_produces_no_items() -> None:
    assert game_context_to_knowledge_items(GameContext()) == []


def test_overview_maps_to_objective() -> None:
    context = GameContext(
        overview=Overview(title="WordVenture", genre="word puzzle", platform="mobile")
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "OBJECTIVE",
            "summary": "WordVenture",
            "description": "genre: word puzzle\nplatform: mobile",
        }
    ]


def test_overview_none_produces_no_item() -> None:
    assert game_context_to_knowledge_items(GameContext(overview=None)) == []


def test_screens_map_to_ui() -> None:
    context = GameContext(
        screens=[
            Screen(
                name="Shop",
                purpose="구매",
                elements=["gold counter", "buy button"],
                transitions=["Inventory"],
            )
        ]
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "UI",
            "summary": "Shop",
            "description": (
                "purpose: 구매\n"
                "elements: gold counter, buy button\n"
                "transitions: Inventory"
            ),
        }
    ]


def test_mechanics_map_to_rule() -> None:
    context = GameContext(
        mechanics=[
            Mechanic(
                name="Purchase",
                description="소지금으로 아이템을 산다",
                rules=["가격 이상 소지금 필요"],
                preconditions=["상점 화면"],
            )
        ]
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "RULE",
            "summary": "Purchase",
            "description": (
                "description: 소지금으로 아이템을 산다\n"
                "rules: 가격 이상 소지금 필요\n"
                "preconditions: 상점 화면"
            ),
        }
    ]


def test_entities_map_to_misc() -> None:
    context = GameContext(
        entities=[Entity(name="Slime", type="monster", attributes=["weakness: fire"])]
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "MISC",
            "summary": "Slime",
            "description": "type: monster\nattributes: weakness: fire",
        }
    ]


def test_progression_maps_to_objective() -> None:
    context = GameContext(
        progression=[ProgressionItem(name="Tutorial", order=1, notes="첫 화면")]
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "OBJECTIVE",
            "summary": "Tutorial",
            "description": "order: 1\nnotes: 첫 화면",
        }
    ]


def test_progression_order_zero_is_kept() -> None:
    # order 는 0 도 유효한 값이다 — falsy 라고 지워서는 안 된다.
    context = GameContext(progression=[ProgressionItem(name="Intro", order=0, notes="시작")])

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "OBJECTIVE",
            "summary": "Intro",
            "description": "order: 0\nnotes: 시작",
        }
    ]


def test_flows_map_to_control() -> None:
    context = GameContext(
        flows=[Flow(name="Checkout", steps=["Cart 열기", "결제 버튼 누르기"])]
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "CONTROL",
            "summary": "Checkout",
            "description": "steps: Cart 열기, 결제 버튼 누르기",
        }
    ]


def test_glossary_maps_to_misc() -> None:
    context = GameContext(glossary=[GlossaryItem(term="Aggro", meaning="적이 플레이어를 노림")])

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {
            "tag": "MISC",
            "summary": "Aggro",
            "description": "meaning: 적이 플레이어를 노림",
        }
    ]


def test_misc_summary_is_first_line_truncated_to_80_chars() -> None:
    long_line = "x" * 100
    context = GameContext(misc=[MiscItem(note=long_line)])

    items = game_context_to_knowledge_items(context)

    assert len(items) == 1
    assert items[0].summary == long_line[:80]
    assert items[0].description == long_line


def test_misc_summary_stops_at_first_newline() -> None:
    context = GameContext(misc=[MiscItem(note="첫 줄\n둘째 줄")])

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {"tag": "MISC", "summary": "첫 줄", "description": "첫 줄\n둘째 줄"}
    ]


def test_items_with_empty_summary_or_description_are_dropped() -> None:
    context = GameContext(
        overview=Overview(title=""),  # summary 가 비어 걸러진다
        screens=[Screen(name="Empty screen")],  # description 이 비어 걸러진다
        entities=[Entity(name="Boss", type="monster")],  # 둘 다 채워져 남는다
    )

    items = [item.model_dump() for item in game_context_to_knowledge_items(context)]

    assert items == [
        {"tag": "MISC", "summary": "Boss", "description": "type: monster"}
    ]


def test_all_eight_sections_survive_conversion() -> None:
    context = GameContext(
        overview=Overview(title="Overview title", genre="genre"),
        screens=[Screen(name="Screen", purpose="purpose")],
        mechanics=[Mechanic(name="Mechanic", description="mechanic description")],
        entities=[Entity(name="Entity", type="entity type")],
        progression=[ProgressionItem(name="Progression", order=1)],
        flows=[Flow(name="Flow", steps=["step"])],
        glossary=[GlossaryItem(term="Term", meaning="meaning")],
        misc=[MiscItem(note="misc note")],
    )

    items = game_context_to_knowledge_items(context)

    summaries = {item.summary for item in items}
    assert summaries == {
        "Overview title",
        "Screen",
        "Mechanic",
        "Entity",
        "Progression",
        "Flow",
        "Term",
        "misc note",
    }


def test_every_item_tag_is_a_known_knowledge_tag() -> None:
    context = GameContext(
        overview=Overview(title="Overview title", genre="genre"),
        screens=[Screen(name="Screen", purpose="purpose")],
        mechanics=[Mechanic(name="Mechanic", description="mechanic description")],
        entities=[Entity(name="Entity", type="entity type")],
        progression=[ProgressionItem(name="Progression", order=1)],
        flows=[Flow(name="Flow", steps=["step"])],
        glossary=[GlossaryItem(term="Term", meaning="meaning")],
        misc=[MiscItem(note="misc note")],
    )

    items = game_context_to_knowledge_items(context)

    assert all(item.tag in KNOWLEDGE_TAGS for item in items)


def test_game_context_sections_are_the_eight_this_module_reads() -> None:
    # GameContext 에 아홉 번째 section 이 생기면, 변환 함수가 조용히 빠뜨리는
    # 대신 이 테스트가 먼저 깨진다.
    assert set(GameContext.model_fields) == {
        "overview",
        "screens",
        "mechanics",
        "entities",
        "progression",
        "flows",
        "glossary",
        "misc",
    }
