"""매크로의 키 인자가 쓰는 Unity `KeyCode` 이름과, 틀린 이름을 고치는 안내 (ARTEL-1432).

SDK 는 `press_key`·`hold_key`·`release_key` 의 키를 `UnityEngine.KeyCode` 이름으로 읽는다(대소문자 무관,
`ActionExecutor.TryReadKeyCode`). 목록에 없는 이름은 SDK 가 런에서야 거절한다. 2026-11-10 MCP 시험에서 키 사용 약 84번
중 8번이 `enter`, `left shift` 같은 이름이었고 저장 검사를 그대로 지나갔다. 그래서 저장하기 전에 parser 가 막는다.

목록은 Unity 2022.3 에디터의 `UnityEngine.CoreModule.xml` 에 있는 `KeyCode` 의 모든 이름이다(`None` 은 키가 아니라 뺐다).
별칭(`LeftCommand`·`LeftApple`)도 각자 이름으로 들어 있다.
"""

KEY_CODE_NAMES: frozenset[str] = frozenset(
    (
        "A", "Alpha0", "Alpha1", "Alpha2", "Alpha3", "Alpha4", "Alpha5", "Alpha6", "Alpha7", "Alpha8",
        "Alpha9", "AltGr", "Ampersand", "Asterisk", "At", "B", "BackQuote", "Backslash", "Backspace",
        "Break", "C", "CapsLock", "Caret", "Clear", "Colon", "Comma", "D", "Delete", "Dollar",
        "DoubleQuote", "DownArrow", "E", "End", "Equals", "Escape", "Exclaim", "F", "F1", "F10", "F11",
        "F12", "F13", "F14", "F15", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "G", "Greater", "H",
        "Hash", "Help", "Home", "I", "Insert", "J", "Joystick1Button0", "Joystick1Button1",
        "Joystick1Button10", "Joystick1Button11", "Joystick1Button12", "Joystick1Button13",
        "Joystick1Button14", "Joystick1Button15", "Joystick1Button16", "Joystick1Button17",
        "Joystick1Button18", "Joystick1Button19", "Joystick1Button2", "Joystick1Button3",
        "Joystick1Button4", "Joystick1Button5", "Joystick1Button6", "Joystick1Button7",
        "Joystick1Button8", "Joystick1Button9", "Joystick2Button0", "Joystick2Button1",
        "Joystick2Button10", "Joystick2Button11", "Joystick2Button12", "Joystick2Button13",
        "Joystick2Button14", "Joystick2Button15", "Joystick2Button16", "Joystick2Button17",
        "Joystick2Button18", "Joystick2Button19", "Joystick2Button2", "Joystick2Button3",
        "Joystick2Button4", "Joystick2Button5", "Joystick2Button6", "Joystick2Button7",
        "Joystick2Button8", "Joystick2Button9", "Joystick3Button0", "Joystick3Button1",
        "Joystick3Button10", "Joystick3Button11", "Joystick3Button12", "Joystick3Button13",
        "Joystick3Button14", "Joystick3Button15", "Joystick3Button16", "Joystick3Button17",
        "Joystick3Button18", "Joystick3Button19", "Joystick3Button2", "Joystick3Button3",
        "Joystick3Button4", "Joystick3Button5", "Joystick3Button6", "Joystick3Button7",
        "Joystick3Button8", "Joystick3Button9", "Joystick4Button0", "Joystick4Button1",
        "Joystick4Button10", "Joystick4Button11", "Joystick4Button12", "Joystick4Button13",
        "Joystick4Button14", "Joystick4Button15", "Joystick4Button16", "Joystick4Button17",
        "Joystick4Button18", "Joystick4Button19", "Joystick4Button2", "Joystick4Button3",
        "Joystick4Button4", "Joystick4Button5", "Joystick4Button6", "Joystick4Button7",
        "Joystick4Button8", "Joystick4Button9", "Joystick5Button0", "Joystick5Button1",
        "Joystick5Button10", "Joystick5Button11", "Joystick5Button12", "Joystick5Button13",
        "Joystick5Button14", "Joystick5Button15", "Joystick5Button16", "Joystick5Button17",
        "Joystick5Button18", "Joystick5Button19", "Joystick5Button2", "Joystick5Button3",
        "Joystick5Button4", "Joystick5Button5", "Joystick5Button6", "Joystick5Button7",
        "Joystick5Button8", "Joystick5Button9", "Joystick6Button0", "Joystick6Button1",
        "Joystick6Button10", "Joystick6Button11", "Joystick6Button12", "Joystick6Button13",
        "Joystick6Button14", "Joystick6Button15", "Joystick6Button16", "Joystick6Button17",
        "Joystick6Button18", "Joystick6Button19", "Joystick6Button2", "Joystick6Button3",
        "Joystick6Button4", "Joystick6Button5", "Joystick6Button6", "Joystick6Button7",
        "Joystick6Button8", "Joystick6Button9", "Joystick7Button0", "Joystick7Button1",
        "Joystick7Button10", "Joystick7Button11", "Joystick7Button12", "Joystick7Button13",
        "Joystick7Button14", "Joystick7Button15", "Joystick7Button16", "Joystick7Button17",
        "Joystick7Button18", "Joystick7Button19", "Joystick7Button2", "Joystick7Button3",
        "Joystick7Button4", "Joystick7Button5", "Joystick7Button6", "Joystick7Button7",
        "Joystick7Button8", "Joystick7Button9", "Joystick8Button0", "Joystick8Button1",
        "Joystick8Button10", "Joystick8Button11", "Joystick8Button12", "Joystick8Button13",
        "Joystick8Button14", "Joystick8Button15", "Joystick8Button16", "Joystick8Button17",
        "Joystick8Button18", "Joystick8Button19", "Joystick8Button2", "Joystick8Button3",
        "Joystick8Button4", "Joystick8Button5", "Joystick8Button6", "Joystick8Button7",
        "Joystick8Button8", "Joystick8Button9", "JoystickButton0", "JoystickButton1",
        "JoystickButton10", "JoystickButton11", "JoystickButton12", "JoystickButton13",
        "JoystickButton14", "JoystickButton15", "JoystickButton16", "JoystickButton17",
        "JoystickButton18", "JoystickButton19", "JoystickButton2", "JoystickButton3", "JoystickButton4",
        "JoystickButton5", "JoystickButton6", "JoystickButton7", "JoystickButton8", "JoystickButton9",
        "K", "Keypad0", "Keypad1", "Keypad2", "Keypad3", "Keypad4", "Keypad5", "Keypad6", "Keypad7",
        "Keypad8", "Keypad9", "KeypadDivide", "KeypadEnter", "KeypadEquals", "KeypadMinus",
        "KeypadMultiply", "KeypadPeriod", "KeypadPlus", "L", "LeftAlt", "LeftApple", "LeftArrow",
        "LeftBracket", "LeftCommand", "LeftControl", "LeftCurlyBracket", "LeftMeta", "LeftParen",
        "LeftShift", "LeftWindows", "Less", "M", "Menu", "Minus", "Mouse0", "Mouse1", "Mouse2",
        "Mouse3", "Mouse4", "Mouse5", "Mouse6", "N", "Numlock", "O", "P", "PageDown", "PageUp", "Pause",
        "Percent", "Period", "Pipe", "Plus", "Print", "Q", "Question", "Quote", "R", "Return",
        "RightAlt", "RightApple", "RightArrow", "RightBracket", "RightCommand", "RightControl",
        "RightCurlyBracket", "RightMeta", "RightParen", "RightShift", "RightWindows", "S", "ScrollLock",
        "Semicolon", "Slash", "Space", "SysReq", "T", "Tab", "Tilde", "U", "Underscore", "UpArrow", "V",
        "W", "X", "Y", "Z",
    )
)

_BY_LOWER = {name.lower(): name for name in KEY_CODE_NAMES}

# 사람이 흔히 쓰는 이름 → KeyCode 이름. 공백·밑줄·하이픈을 지우고 소문자로 맞춘 뒤 찾는다.
# Unity legacy 문자열 이름에서 "enter" 는 숫자패드 Enter 지만, 매크로를 쓰는 쪽은 거의 언제나 본 Enter 를 뜻한다.
_ALIASES = {
    "enter": "Return",
    "ctrl": "LeftControl",
    "control": "LeftControl",
    "leftctrl": "LeftControl",
    "rightctrl": "RightControl",
    "shift": "LeftShift",
    "alt": "LeftAlt",
    "option": "LeftAlt",
    "cmd": "LeftCommand",
    "command": "LeftCommand",
    "meta": "LeftMeta",
    "win": "LeftWindows",
    "windows": "LeftWindows",
    "esc": "Escape",
    "del": "Delete",
    "ins": "Insert",
    "pgup": "PageUp",
    "pgdn": "PageDown",
    "up": "UpArrow",
    "down": "DownArrow",
    "left": "LeftArrow",
    "right": "RightArrow",
    "spacebar": "Space",
    "capslock": "CapsLock",
    "printscreen": "Print",
    "backtick": "BackQuote",
    "grave": "BackQuote",
}
_ALIASES.update({str(digit): f"Alpha{digit}" for digit in range(10)})
_ALIASES.update({f"numpad{digit}": f"Keypad{digit}" for digit in range(10)})

KEY_INPUT_RULES = (
    "Key names are Unity `KeyCode` names, matched without case: `Space`, `Return` (not `enter`), `Escape`, "
    "`LeftShift`, `LeftControl`, `LeftAlt`, `UpArrow`, `Alpha1` for the 1 key, `Keypad1` for the keypad. A name "
    "that is not a `KeyCode` is refused. Check how the game reads the input before choosing the action: a key read "
    "with `Input.GetKey*` takes `press_key`/`hold_key`; an Input Manager axis or button read with `Input.GetAxis` "
    "or `Input.GetButton*` (also through wrappers such as `CrossPlatformInputManager`) takes "
    "`set_input_axis(\"<axis>\", <value>)` or `set_input_button(\"<axis>\", True)` with that axis name, because a "
    "virtual key never reaches an axis; an Input System action takes `press_key` with a key bound to it."
)


def canonical_key_code(name: str) -> str | None:
    """`name` 이 KeyCode 이름이면 정규 표기를, 아니면 None 을 돌려준다. 대소문자는 가리지 않는다."""
    return _BY_LOWER.get(name.strip().lower())


def key_code_suggestion(name: str) -> str | None:
    """KeyCode 가 아닌 `name` 에 가장 가까운 KeyCode 이름. 없으면 None."""
    squeezed = "".join(character for character in name.lower() if character not in " _-")
    return _ALIASES.get(squeezed) or _BY_LOWER.get(squeezed)
