"""`GET /internal/macros/grammar` 와 `POST /internal/macros/check` (ARTEL-1087)."""

from fastapi.testclient import TestClient

from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY
from app.main import app

client = TestClient(app)

OPEN_SHOP = '''def open_shop() -> None:
    """상점 화면을 연다. 메인 화면에서 상점 물건을 볼 때 쓴다."""
    click(selector("Root[0]/Canvas[1]/Shop[2]"))
'''

BUY_ITEM = '''from macros import open_shop

def buy_item(item: object, count: int) -> None:
    """상점에서 물건 하나를 산다. 상점이 열려 있지 않아도 된다."""
    open_shop()
    click(item)
'''


def _check(name: str, source: str, imports: dict[str, str] | None = None) -> dict:
    response = client.post(
        "/internal/macros/check",
        json={"name": name, "source": source, "imports": imports or {}},
    )
    assert response.status_code == 200
    return response.json()


def test_grammar_is_the_qa_grammar_with_developer_rules() -> None:
    response = client.get("/internal/macros/grammar")

    assert response.status_code == 200
    body = response.json()
    assert body["grammar"] == GRAMMAR + "\n\n" + NUMBER_EQUALITY
    assert body["rules"] and all(isinstance(rule, str) for rule in body["rules"])
    joined = "\n".join(body["rules"])
    for must_say in ("-> None", "docstring", "from macros import", "#id"):
        assert must_say in joined


def test_a_valid_macro_returns_definition_parameters_and_docstring() -> None:
    body = _check("buy_item", BUY_ITEM, {"open_shop": OPEN_SHOP})

    assert body["ok"] is True
    assert body["parameters"] == ["item", "count"]
    assert body["docstring"] == "상점에서 물건 하나를 산다. 상점이 열려 있지 않아도 된다."
    definition = body["definition"]
    assert definition["name"] == "buy_item"
    assert definition["source"] == BUY_ITEM
    assert definition["entry"]["name"] == "buy_item"
    assert [imported["name"] for imported in definition["imports"]] == ["open_shop"]


def test_a_parser_refusal_carries_its_line() -> None:
    source = '''def m() -> None:
    """설명."""
    click(target.child)
'''
    body = _check("m", source)

    assert body["ok"] is False
    assert body["rejection"]["line"] == 3
    assert body["rejection"]["reason"].startswith("MACRO_NODE_REJECTED (line 3) — ")


def test_an_entry_without_a_docstring_is_refused_at_its_def_line() -> None:
    source = '''from macros import open_shop

def m() -> None:
    open_shop()
'''
    body = _check("m", source, {"open_shop": OPEN_SHOP})

    assert body["ok"] is False
    assert body["rejection"]["line"] == 3
    assert "no docstring" in body["rejection"]["reason"]


def test_an_entry_docstring_over_2000_characters_is_refused_at_its_def_line() -> None:
    def source(docstring_length: int) -> str:
        docstring = "가" * docstring_length
        return f'''from macros import open_shop

def m() -> None:
    """{docstring}"""
    open_shop()
'''

    at_limit = _check("m", source(2_000), {"open_shop": OPEN_SHOP})
    over_limit = _check("m", source(2_001), {"open_shop": OPEN_SHOP})

    assert at_limit["ok"] is True
    assert over_limit["ok"] is False
    assert over_limit["rejection"]["line"] == 3
    assert "limit of 2000" in over_limit["rejection"]["reason"]


def test_an_import_is_resolved_through_its_own_imports() -> None:
    restock = '''from macros import buy_item

def restock(item: object) -> None:
    """물건을 다시 채운다."""
    buy_item(item, 3)
'''
    body = _check("restock", restock, {"buy_item": BUY_ITEM, "open_shop": OPEN_SHOP})

    assert body["ok"] is True
    [buy] = body["definition"]["imports"]
    assert [imported["name"] for imported in buy["imports"]] == ["open_shop"]


def test_a_missing_import_is_refused_by_name() -> None:
    body = _check("buy_item", BUY_ITEM)

    assert body["ok"] is False
    assert body["rejection"]["line"] == 1
    assert "`open_shop`" in body["rejection"]["reason"]
    assert "no developer macro by that name" in body["rejection"]["reason"]


def test_a_missing_import_two_levels_down_names_both_macros() -> None:
    restock = '''from macros import buy_item

def restock(item: object) -> None:
    """물건을 다시 채운다."""
    buy_item(item, 3)
'''
    body = _check("restock", restock, {"buy_item": BUY_ITEM})

    assert body["ok"] is False
    reason = body["rejection"]["reason"]
    assert "`buy_item` is imported but is itself refused" in reason
    assert "`open_shop`" in reason


def test_an_import_cycle_is_refused() -> None:
    ping = '''from macros import pong

def ping() -> None:
    """핑."""
    pong()
'''
    pong = '''from macros import ping

def pong() -> None:
    """퐁."""
    ping()
'''
    body = _check("ping", ping, {"ping": ping, "pong": pong})

    assert body["ok"] is False
    assert "ping → pong → ping" in body["rejection"]["reason"]


def test_an_unstable_selector_is_refused_with_its_line() -> None:
    source = '''def m() -> None:
    """설명."""
    click(selector("#12345"))
'''
    body = _check("m", source)

    assert body["ok"] is False
    assert body["rejection"]["line"] == 3
    assert "`#id`" in body["rejection"]["reason"]


def test_the_entry_def_must_be_named_like_the_macro() -> None:
    body = _check("other_name", OPEN_SHOP)

    assert body["ok"] is False
    assert "`def other_name(...)`" in body["rejection"]["reason"]


def test_an_oversized_source_is_a_malformed_body() -> None:
    response = client.post(
        "/internal/macros/check",
        json={"name": "m", "source": "#" * 20_001, "imports": {}},
    )

    assert response.status_code == 422


def test_an_oversized_name_is_a_malformed_body() -> None:
    response = client.post(
        "/internal/macros/check",
        json={"name": "m" * 201, "source": OPEN_SHOP, "imports": {}},
    )

    assert response.status_code == 422
