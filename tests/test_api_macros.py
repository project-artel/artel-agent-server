"""`GET /internal/macros/grammar` 와 `POST /internal/macros/check` (ARTEL-1087)."""

from fastapi.testclient import TestClient

from app.agents.qa.macro.grammar import TOOLS
from app.developer_macro_grammar import tool_signature
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


def test_grammar_is_written_for_the_developer_with_developer_rules() -> None:
    response = client.get("/internal/macros/grammar")

    assert response.status_code == 200
    body = response.json()
    grammar = body["grammar"]
    # 개발자에게 없는 QA agent 도구는 부르지 않는다.
    for qa_only in (
        "register_macro",
        "edit_macro",
        "write_macro",
        "run_macro",
        "resume_macro",
        "search_macros",
    ):
        assert qa_only not in grammar
    # action 마다 인자 이름·순서·기본값을 적는다.
    for spec in TOOLS:
        assert f"`{tool_signature(spec)}`" in grammar
    assert "drag(from_target: object, to_target: object, button: int = 0)" in grammar
    # 개발자가 게임 코드를 보고 먼저 묻는 것들.
    for must_say in (
        "KeyCode",
        "LeftShift",
        "OnMouseDown",
        "Input.anyKeyDown",
        "DontDestroyOnLoad",
        "Sibling indices are optional",
        "TextMeshPro",
        "never `find(...)` inline",
        # 실측(try 180): 글자 자식을 누르면 닿지 않고, 효과 확인이 없으면 통과로 끝난다.
        "Aim at the object that receives input",
        "`reached` is null",
        "with a `wait_until` or `require` on the effect",
    ):
        # 글이 80자에서 줄을 바꾸므로 공백을 하나로 접어 찾는다.
        assert must_say in " ".join(grammar.split())
    # 예시가 그 두 습관을 따르고, 검사도 통과한다.
    example = grammar.split("```python\n")[1].split("```")[0]
    assert "find(label=" not in example
    assert "wait_until(" in example
    assert _check("place_spell_card", example)["ok"] is True
    assert body["rules"] and all(isinstance(rule, str) for rule in body["rules"])
    joined = "\n".join(body["rules"])
    for must_say in ("-> None", "docstring", "from macros import", "#id", "KeyCode"):
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


def _with_key(statement: str) -> str:
    return f'''def tap() -> None:
    """키를 누른다."""
    {statement}
'''


def test_a_key_code_that_is_not_a_unity_keycode_is_refused_with_a_suggestion() -> None:
    body = _check("tap", _with_key('hold_key("shift")\n    release_key("LeftShift")'))

    assert body["ok"] is False
    assert body["rejection"]["line"] == 3
    assert "KeyCode" in body["rejection"]["reason"]
    assert 'Write "LeftShift"' in body["rejection"]["reason"]


def test_a_key_code_is_read_without_case_and_also_by_keyword() -> None:
    for statement in (
        'press_key("space", 0.1)',
        'press_key(key_code="Return", duration_seconds=0.1)',
        'press_key("Alpha1", 0.1)',
    ):
        assert _check("tap", _with_key(statement))["ok"] is True, statement

    body = _check("tap", _with_key('press_key(key_code="enter", duration_seconds=0.1)'))
    assert body["ok"] is False
    assert 'Write "Return"' in body["rejection"]["reason"]


def test_a_key_code_passed_as_a_parameter_is_not_checked() -> None:
    source = '''def tap(key: string) -> None:
    """받은 키를 누른다."""
    press_key(key, 0.1)
'''
    assert _check("tap", source)["ok"] is True


def test_the_check_refuses_a_selector_recent_runs_almost_never_saw_live() -> None:
    """orchestration 이 그 build 의 최근 QA 런 `pulse` 에서 센 경로를 `observed` 로 싣는다."""
    source = '''def look() -> None:
    """Looks at the tutorial line."""
    if exists(selector("Tutorial/ChatWindow/ChatText")):
        press_key("Space", 0.1)
'''
    response = client.post(
        "/internal/macros/check",
        json={"name": "look", "source": source, "observed": {"Tutorial/ChatWindow/Panel/ChatText": 300}},
    )

    body = response.json()
    assert body["ok"] is False
    assert body["rejection"]["line"] == 3
    assert "`Tutorial/ChatWindow/Panel/ChatText` (300)" in body["rejection"]["reason"]
