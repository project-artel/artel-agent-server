"""새로 쓰는 macro 에 게임 실행마다 바뀌는 selector 가 들어가는 것을 막는다 (ARTEL-1026).

`#id` 의 instance id 는 게임을 다시 띄우면 바뀐다. `-` 와 빈 문자열은 아무것도 겨누지 않는다.
parser 는 이미 저장된 row 를 읽을 때도 쓰이므로 이 검사를 넣지 않는다. 쓰는 길
(`write_macro`, `edit_macro`, `register_macro`) 만 `reject_unstable_selectors` 를 부른다.
"""

import ast

from app.agents.qa.macro.errors import MacroRejection

UNSTABLE_SELECTOR_REASON = (
    "a `#id` changes between game launches, and `-` or an empty string names nothing, so a "
    "stored macro that aims with one fails on the next run. Aim with "
    "`find(label=..., name=...)` or a hierarchy path such as "
    'selector("Canvas/StartButton"); sibling indices such as '
    '"Canvas[1]/StartButton[2]" are optional.'
)


def is_unstable_selector(text: str) -> bool:
    stripped = text.strip()
    return not stripped or stripped == "-" or stripped.startswith("#")


def reject_unstable_selectors(source: str) -> None:
    """`selector("...")` 리터럴이 불안정하면 줄 번호와 함께 `MacroRejection` 을 던진다.

    문법 오류는 여기서 다루지 않는다. 그건 `macro_definition_from_source` 가 이미 거절한다.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "selector"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        if is_unstable_selector(node.args[0].value):
            raise MacroRejection(
                f"`selector({node.args[0].value!r})` is refused: {UNSTABLE_SELECTOR_REASON}",
                node.lineno,
            )
