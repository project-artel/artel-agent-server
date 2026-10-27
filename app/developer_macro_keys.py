"""developer macro 의 `key_code` 리터럴이 Unity `KeyCode` 이름인지 publish 때 본다.

SDK 는 `key_code` 를 `Enum.TryParse(value, ignoreCase: true)` 로 읽고, 못 읽으면 그 action 을
거절한다(`ActionExecutor.TryReadKeyCode`). `"shift"`·`"enter"` 처럼 흔히 쓰는 이름이 그렇게
QA 런에서야 실패하므로, 개발자가 쓰는 길에서는 리터럴을 미리 대 본다.

QA 런의 tool loop 에는 넣지 않는다. QA agent 는 tool 설명의 `KeyCode` 예시를 보고 쓰고, 거기서
틀리면 다음 턴에 SDK 의 거절을 읽고 고친다. parameter 로 받은 이름은 값을 모르므로 보지 않는다.
"""

import ast

from app.agents.qa.macro.errors import MacroRejection

# Unity 6000.5 `UnityEngine.KeyCode` 의 이름 전부(`None` 제외). 2022.3 에는 `WheelUp`·`WheelDown`
# 만 없다 — 그 둘을 쓴 macro 는 2022.3 게임에서 SDK 가 거절하지만, 둘을 위해 버전을 묻지는 않는다.
_NAMED_KEYS = (
    "Backspace Delete Tab Clear Return Pause Escape Space "
    "KeypadPeriod KeypadDivide KeypadMultiply KeypadMinus KeypadPlus KeypadEnter KeypadEquals "
    "UpArrow DownArrow RightArrow LeftArrow Insert Home End PageUp PageDown "
    "Exclaim DoubleQuote Hash Dollar Percent Ampersand Quote LeftParen RightParen Asterisk Plus "
    "Comma Minus Period Slash Colon Semicolon Less Equals Greater Question At LeftBracket "
    "Backslash RightBracket Caret Underscore BackQuote LeftCurlyBracket Pipe RightCurlyBracket "
    "Tilde Numlock CapsLock ScrollLock RightShift LeftShift RightControl LeftControl RightAlt "
    "LeftAlt LeftMeta LeftCommand LeftApple LeftWindows RightMeta RightCommand RightApple "
    "RightWindows AltGr Help Print SysReq Break Menu WheelUp WheelDown"
).split()

UNITY_KEY_CODES: frozenset[str] = frozenset(
    [
        *_NAMED_KEYS,
        *(chr(code) for code in range(ord("A"), ord("Z") + 1)),
        *(f"Alpha{digit}" for digit in range(10)),
        *(f"Keypad{digit}" for digit in range(10)),
        *(f"F{number}" for number in range(1, 25)),
        *(f"Mouse{number}" for number in range(7)),
        *(f"JoystickButton{number}" for number in range(20)),
        *(
            f"Joystick{joystick}Button{number}"
            for joystick in range(1, 9)
            for number in range(20)
        ),
    ]
)
_BY_LOWER = {name.lower(): name for name in UNITY_KEY_CODES}

# 흔히 쓰지만 `KeyCode` 이름이 아닌 것 → 고쳐 쓸 이름. 거절 문장이 바로 대 준다.
_COMMON_MISTAKES = {
    "shift": "LeftShift",
    "ctrl": "LeftControl",
    "control": "LeftControl",
    "alt": "LeftAlt",
    "cmd": "LeftCommand",
    "command": "LeftCommand",
    "enter": "Return",
    "esc": "Escape",
    "up": "UpArrow",
    "down": "DownArrow",
    "left": "LeftArrow",
    "right": "RightArrow",
    "spacebar": "Space",
    **{str(digit): f"Alpha{digit}" for digit in range(10)},
}

# `key_code` 를 받는 tool. 이름이 `grammar.TOOLS` 의 parameter 이름과 같아야 한다.
_KEY_TOOLS = ("press_key", "hold_key", "release_key")
_KEY_PARAMETER = "key_code"


def reject_unknown_key_codes(source: str) -> None:
    """`key_code` 자리의 문자열 리터럴이 `KeyCode` 이름이 아니면 줄 번호와 함께 거절한다."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _KEY_TOOLS
        ):
            continue
        written = _key_argument(node)
        if not (isinstance(written, ast.Constant) and isinstance(written.value, str)):
            continue
        if written.value.strip().lower() in _BY_LOWER:
            continue
        raise MacroRejection(_unknown_key_reason(node.func.id, written.value), node.lineno)


def _key_argument(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg == _KEY_PARAMETER:
            return keyword.value
    return None


def _unknown_key_reason(tool: str, written: str) -> str:
    suggestion = _COMMON_MISTAKES.get(written.strip().lower())
    instead = f' Write "{suggestion}".' if suggestion else ""
    return (
        f"`{tool}({written!r}, ...)` names no Unity `KeyCode`, so the game would refuse "
        f"the key when a QA run calls this macro.{instead} `key_code` is a `KeyCode` name "
        'such as "Space", "Return", "Escape", "LeftShift", "A", "Alpha1" or "F1"; case '
        "does not matter."
    )
