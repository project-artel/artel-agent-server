"""macro 문법이 받는 것 전부, 데이터로.

여기 든 것은 표와 목록뿐이고 판단은 없다. parser 는 이 표를 찾아 거절하고, runner 는
이 표를 찾아 `JsonRpcAction` 을 조립하고, 모델이 읽는 설명(`descriptions.py`)은 이
표에서 이름을 뽑아 쓴다. 세 쪽이 각자 목록을 들면 언제나 한쪽만 고쳐진다.

연산자를 타입마다 따로 주는 이유가 이 파일의 모양이다. 표의 칸마다 허용이 있거나
거절이 있어서, 새 모양이 들어오면 행이 하나 느는 일이 되고 빠뜨린 칸이 눈에 보인다.
연산자 하나에 `if` 하나를 다는 모양이면 빠뜨린 칸은 아무 데도 안 보인다.
"""

from dataclasses import dataclass
from enum import StrEnum


class MacroType(StrEnum):
    """`def` 줄과 대입에 적는 타입 이름.

    일곱을 든다. 선언할 수 있는 것은 아래 `DECLARABLE_TYPES` 의 다섯이고,
    `vector2` 와 `vector3` 는 선언 자리에서 거절된다 — v1 에 vector 를 만들 문법도
    비교할 연산자도 없어 선언해 봐야 할 수 있는 일이 없다. 두 이름이 여기 남는 것은
    도착한 값의 모양 이름으로 쓰기 때문이다(`MacroShape`).
    """

    int_ = "int"
    float_ = "float"
    string = "string"
    bool_ = "bool"
    vector2 = "vector2"
    vector3 = "vector3"
    object_ = "object"


DECLARABLE_TYPES: tuple[MacroType, ...] = (
    MacroType.int_,
    MacroType.float_,
    MacroType.string,
    MacroType.bool_,
    MacroType.object_,
)
UNDECLARABLE_TYPES: tuple[MacroType, ...] = (MacroType.vector2, MacroType.vector3)

# 거절 문장에 그대로 끼우는 문구. 타입을 안 적었거나 `vector2` 를 적었을 때, 받을 수
# 있는 다섯을 이름으로 전부 대야 모델이 한 번에 고친다.
DECLARABLE_TYPE_NAMES = ", ".join(declared.value for declared in DECLARABLE_TYPES)


class MacroShape(StrEnum):
    """**도착한** 값의 모양. 선언 타입과 다른 층이다.

    선언은 `int` 와 `float` 를 가르지만 여기서는 둘 다 `number` 다. SDK 의 `Number`
    (`LiveState.cs:1533`)가 `Math.Round(value, 4).ToString("0.####")` 로 쓰므로 float
    `3.0` 이 `3` 으로 도착해 JSON 에서 int 로 읽힌다. 값만 보고 int 와 float 를 가릴 수
    없으니 두 행으로 나눈 표는 집행이 안 된다.

    `other` 는 숫자도 글자도 참거짓도 vector 도 객체도 아닌 것 전부다. sprite,
    animator state, 그리고 `Number` 가 NaN 과 Infinity 대신 보내는
    `{"unread": "not-a-number"}` 모양이 여기 든다.
    """

    number = "number"
    string = "string"
    bool_ = "bool"
    vector2 = "vector2"
    vector3 = "vector3"
    object_ = "object"
    other = "other"


class MacroOperator(StrEnum):
    equal = "=="
    not_equal = "!="
    greater = ">"
    less = "<"
    greater_or_equal = ">="
    less_or_equal = "<="


EQUALITY_OPERATORS: tuple[MacroOperator, ...] = (MacroOperator.equal, MacroOperator.not_equal)
ORDER_OPERATORS: tuple[MacroOperator, ...] = (
    MacroOperator.greater,
    MacroOperator.less,
    MacroOperator.greater_or_equal,
    MacroOperator.less_or_equal,
)

# 표의 일곱 행. 적힌 것이 되는 것이고, 안 적힌 것은 거절이다.
#
# - number: 전부 된다. 순서 비교가 되는 모양은 이것 하나다. `==` 도 되지만 위태롭다 —
#   연속값(위치·타이머)은 네 자리 반올림 뒤에도 마지막 자리가 오르내린다. 권고는
#   `write_macro` 의 설명에 싣고, 거절은 하지 않는다.
# - string·bool: 순서 비교는 뜻이 없다.
# - vector2·vector3: 전부 거절한다. `>` 가 성분별인지 크기인지는 추측이고, `==` 는
#   number 의 위태로움이 성분마다 겹친다. 선언 자리에서도 거절하므로 이 두 행에 닿는
#   것은 평가 시점에 도착한 값뿐이다.
# - object: 같음의 기준이 `id` 가 아니라 pulse 키(`PulseObject.key`, `pulse.py:172-174`)
#   다. `PulseObject.id` 가 `int | None` 이라 `id` 로 가르면 `None` 인 기록에 규칙이 없다.
# - other: 전부 거절한다. 조용히 거짓이 되면 `require` 가 게임 결함처럼 실패한다.
_ALLOWED_OPERATORS: dict[MacroShape, tuple[MacroOperator, ...]] = {
    MacroShape.number: EQUALITY_OPERATORS + ORDER_OPERATORS,
    MacroShape.string: EQUALITY_OPERATORS,
    MacroShape.bool_: EQUALITY_OPERATORS,
    MacroShape.vector2: (),
    MacroShape.vector3: (),
    MacroShape.object_: EQUALITY_OPERATORS,
    MacroShape.other: (),
}

# `(값의 모양, 연산자)` 를 키로 허용 또는 거절을 찾는 표. 일곱 곱하기 여섯 = 마흔둘.
COMPARISON_TABLE: dict[tuple[MacroShape, MacroOperator], bool] = {
    (shape, operator): operator in allowed
    for shape, allowed in _ALLOWED_OPERATORS.items()
    for operator in MacroOperator
}

if len(COMPARISON_TABLE) != len(MacroShape) * len(MacroOperator):
    # 행 하나가 빠지면 그 모양의 비교가 조용히 `KeyError` 가 된다. 모양을 하나 더하는
    # 사람이 `_ALLOWED_OPERATORS` 를 안 고쳤다는 것을 import 시점에 알려 준다.
    raise RuntimeError(
        "COMPARISON_TABLE is missing a row: every MacroShape needs an entry in "
        "_ALLOWED_OPERATORS, with one cell per MacroOperator."
    )


def comparison_allowed(shape: MacroShape, operator: MacroOperator) -> bool:
    return COMPARISON_TABLE[(shape, operator)]


def allowed_operators(shape: MacroShape) -> tuple[MacroOperator, ...]:
    """그 모양에서 되는 연산자. 거절 문장이 이것을 이름으로 댄다."""
    return _ALLOWED_OPERATORS[shape]


def operator_names(operators: tuple[MacroOperator, ...]) -> str:
    """거절 문장에 끼우는 문구. 아무것도 안 되는 모양은 그렇다고 말한다."""
    if not operators:
        return "no operator at all"
    return ", ".join(operator.value for operator in operators)


# --- tool 열넷 -----------------------------------------------------------------


@dataclass(frozen=True)
class ToolParameter:
    """macro 문법에서 tool 하나가 받는 인자 한 자리.

    `is_target` 인 자리는 `selector(<문자열 리터럴>)`·`def` 줄의 parameter·`find()` 나
    `selector()` 에 bind 된 이름 셋만 받는다. 맨문자열 좌표(`640,360`)나 id(`#12345`)를
    적을 문법 자체가 없다.

    `as_instance_id` 는 `enter_text` 하나다. 그 tool 은 `target_id: int` 를 받지만
    macro 문법에서는 `target` 을 받으므로, runner 가 bind 된 기록에서 `id` 를 꺼내 넣는다.
    """

    name: str
    declared_type: MacroType
    is_target: bool = False
    as_instance_id: bool = False
    # 안 적어도 되는 자리의 기본값. runner 가 채운다 — 모델이 적은 것만 JSON 에 싣고
    # 기본값은 이 표가 낸다.
    default: int | float | str | bool | None = None
    required: bool = True


@dataclass(frozen=True)
class ActionStep:
    """한 statement 가 만드는 `JsonRpcAction` 하나.

    `arguments` 는 위 `ToolParameter.name` 들이다. target 자리는 조준값으로 펴지고
    (selector 문자열 하나 또는 id 정수 하나), 나머지는 값 하나로 들어간다.

    click 이 셋, double_click 이 다섯, drag 이 넷인 이유는 누르기가 target 을 안 받기
    때문이다. 포인터가 있는 자리에 떨어지므로 먼저 옮긴다 — `action_tools.py` 의
    같은 tool 들이 같은 순서를 쓴다.
    """

    method: str
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    parameters: tuple[ToolParameter, ...]
    steps: tuple[ActionStep, ...]

    def parameter(self, name: str) -> ToolParameter | None:
        for parameter in self.parameters:
            if parameter.name == name:
                return parameter
        return None


_BUTTON = ToolParameter("button", MacroType.int_, default=0, required=False)
_AIM = ActionStep("move_mouse", ("target",))


# `action_tools.py` 의 tool 열여섯 중 열넷. 빠지는 둘은 `click_button` 과 `reset_game`
# 이다. `click_button` 은 deprecated 이고 — `onClick` 을 직접 불러 occlusion 을 안
# 본다 — `reset_game` 은 macro 에서 금지다. 되돌릴 수 없는 것을 적어 둔 글이 부르는
# 것은 macro 가 할 일이 아니다.
TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "click",
        (ToolParameter("target", MacroType.object_, is_target=True), _BUTTON),
        (_AIM, ActionStep("mouse_down", ("button",)), ActionStep("mouse_up", ("button",))),
    ),
    ToolSpec(
        "double_click",
        (ToolParameter("target", MacroType.object_, is_target=True), _BUTTON),
        (
            _AIM,
            ActionStep("mouse_down", ("button",)),
            ActionStep("mouse_up", ("button",)),
            ActionStep("mouse_down", ("button",)),
            ActionStep("mouse_up", ("button",)),
        ),
    ),
    ToolSpec(
        "drag",
        (
            ToolParameter("from_target", MacroType.object_, is_target=True),
            ToolParameter("to_target", MacroType.object_, is_target=True),
            _BUTTON,
        ),
        (
            ActionStep("move_mouse", ("from_target",)),
            ActionStep("mouse_down", ("button",)),
            ActionStep("move_mouse", ("to_target",)),
            ActionStep("mouse_up", ("button",)),
        ),
    ),
    ToolSpec(
        "move_pointer",
        (ToolParameter("target", MacroType.object_, is_target=True),),
        (_AIM,),
    ),
    ToolSpec(
        "press_key",
        (
            ToolParameter("key_code", MacroType.string),
            ToolParameter("duration_seconds", MacroType.float_),
        ),
        (ActionStep("key_click", ("key_code", "duration_seconds")),),
    ),
    ToolSpec(
        "enter_text",
        (
            ToolParameter("target", MacroType.object_, is_target=True, as_instance_id=True),
            ToolParameter("value", MacroType.string),
        ),
        (ActionStep("enter_text", ("target", "value")),),
    ),
    ToolSpec(
        "hold_mouse_button", (_BUTTON,), (ActionStep("mouse_down", ("button",)),)
    ),
    ToolSpec(
        "release_mouse_button", (_BUTTON,), (ActionStep("mouse_up", ("button",)),)
    ),
    ToolSpec(
        "hold_key",
        (ToolParameter("key_code", MacroType.string),),
        (ActionStep("key_down", ("key_code",)),),
    ),
    ToolSpec(
        "release_key",
        (ToolParameter("key_code", MacroType.string),),
        (ActionStep("key_up", ("key_code",)),),
    ),
    ToolSpec(
        "set_input_axis",
        (
            ToolParameter("axis_name", MacroType.string),
            ToolParameter("value", MacroType.float_),
        ),
        (ActionStep("set_axis", ("axis_name", "value")),),
    ),
    ToolSpec(
        "set_input_button",
        (
            ToolParameter("axis_name", MacroType.string),
            ToolParameter("pressed", MacroType.bool_),
        ),
        (ActionStep("set_button", ("axis_name", "pressed")),),
    ),
    ToolSpec("pause_game_time", (), (ActionStep("pause_time"),)),
    ToolSpec("resume_game_time", (), (ActionStep("resume_time"),)),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOLS}
TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOLS)

# 목록에도 helper 에도 없는 이름을 거절할 때 쓰는 문구.
TOOL_NAME_LIST = ", ".join(TOOL_NAMES)

# 거절 문장이 이름을 대야 하는, 일부러 뺀 둘.
FORBIDDEN_TOOLS: dict[str, str] = {
    "click_button": (
        "`click_button` is deprecated — it invokes a Button's `onClick` directly, so it "
        "sees no occlusion. Write `click` instead"
    ),
    "reset_game": (
        "`reset_game` is not allowed in a macro — a stored script must not be able to "
        "throw the run's progress away. Call the tool yourself when a step needs it"
    ),
}


# --- 조건의 왼쪽에 오는 호출 여덟 -----------------------------------------------


@dataclass(frozen=True)
class ReaderSpec:
    """`require` 와 `if` 의 조건에서 값을 읽는 호출.

    `shape` 가 `None` 인 셋(`static`·`observable`·`member`)은 저장 시점에 모양을
    모른다. 그 셋의 값은 `PulseMember.value`(`pulse.py:74-94`)처럼 일부러 `Any` 이고,
    모양은 값이 도착해야 알려진다. 그래서 저장 시점 거절이 닿는 것은 나머지 다섯이고
    평가 시점의 `COMPARISON_REJECTED` 는 사실상 이 셋에만 남는다.
    """

    name: str
    takes_target: bool = False
    takes_key: bool = False
    shape: MacroShape | None = None


READERS: tuple[ReaderSpec, ...] = (
    ReaderSpec("scene", shape=MacroShape.string),
    # 인자는 selector 가 아니라 `observables` 가 찍는 key 이름이라 맨문자열로 받는다.
    ReaderSpec("observable", takes_key=True),
    ReaderSpec("static", takes_key=True),
    # 기본 빌드에서 수를 읽을 자리가 이것뿐이다. `observable()` 은 `GAME_STATE` 전용이고
    # `static()` 은 래칭 플래그뿐이다.
    ReaderSpec("member", takes_target=True, takes_key=True),
    ReaderSpec("actionable", takes_target=True, shape=MacroShape.bool_),
    # 라벨이 무엇으로 바뀌었는지 물을 수 없게 되는 것은 QA 에서 비용이 너무 크다.
    ReaderSpec("text", takes_target=True, shape=MacroShape.string),
    ReaderSpec("exists", takes_target=True, shape=MacroShape.bool_),
    ReaderSpec("absent", takes_target=True, shape=MacroShape.bool_),
)

READERS_BY_NAME: dict[str, ReaderSpec] = {spec.name: spec for spec in READERS}
READER_NAMES: tuple[str, ...] = tuple(spec.name for spec in READERS)
READER_NAME_LIST = ", ".join(f"{name}()" for name in READER_NAMES)


# --- 그 밖의 이름과 상한 --------------------------------------------------------

# 대입의 오른쪽과 조건의 피연산자에 설 수 있는 두 호출. 읽는 함수가 아니라 객체를
# 지목하는 표현이라 위 여덟에 없다.
FIND = "find"
SELECTOR = "selector"

# 보고하지 않는 두 statement. `flag` 는 알릴 뿐이고 `ask_verdict` 는 그 step 에 판정이
# 필요하다고 세운다. 판정은 QA agent 가 한다.
FLAG = "flag"
ASK_VERDICT = "ask_verdict"
REQUIRE = "require"

# `find` 의 keyword. `label`·`name` 중 최소 하나가 있어야 한다.
FIND_KEYWORDS: tuple[str, ...] = ("label", "name", "under")
FIND_REQUIRED_KEYWORDS: tuple[str, ...] = ("label", "name")

# macro 문법에 없는 이름 중, 거절 문장이 왜 없는지 말해 줘야 하는 것들.
REPORTING_NAMES: dict[str, str] = {
    "report_step": (
        "`report_step` is not part of the macro grammar. A verdict needs the scenario, "
        "the earlier steps, what the operator said and the knowledge this run read, and a "
        "macro holds none of those — use `ask_verdict(<expected>)` to say this step "
        "needs judging, and judge it yourself"
    ),
    "report_case": (
        "`report_case` is not part of the macro grammar: a test case's verdict is derived "
        "from its steps' verdicts, so there is nothing for a macro to report"
    ),
    "report_issue": (
        "`report_issue` is not part of the macro grammar. File the defect yourself after "
        "the macro returns; `flag(<message>)` is how a macro tells you it saw something"
    ),
}

@dataclass(frozen=True)
class PairedTools:
    """눌렀으면 풀어야 하는 tool 두 짝.

    `action_tools.py` 의 그 셋이 전부 같은 문장을 docstring 에 들고 있다 — "Nothing
    releases this for you." tool 을 직접 부르는 agent 는 다음 턴에 그 문장을 다시
    읽지만, macro 는 저작 시점에 글이 고정되므로 읽어 줄 다음 턴이 없다. 그래서 저장
    시점에 센다.
    """

    # 사람이 읽는 이름. 거절 문장에 그대로 끼운다.
    counter: str
    opens: str
    closes: str


# 짝을 맞춰야 하는 셋. `set_input_axis` 와 `set_input_button` 은 여기 없다 — 둘은 같은
# tool 을 다른 인자로 다시 부르는 모양이고, 그 인자가 이름일 수 있어 저장 시점에 값을
# 모른다. 두 tool 의 docstring 이 여전히 그것을 말하지만 집행은 못 한다.
PAIRED_TOOLS: tuple[PairedTools, ...] = (
    PairedTools("a held key", "hold_key", "release_key"),
    PairedTools("a held mouse button", "hold_mouse_button", "release_mouse_button"),
    PairedTools("frozen game time", "pause_game_time", "resume_game_time"),
)


# 호출 깊이. 진입점을 포함해 센다. payload 가 호출 사슬을 찍는데 네 겹은 한 payload
# 에서 안 읽힌다.
MAX_CALL_DEPTH = 3
# 가장 많이 도는 경로의 statement 총수. 32 곱하기 4 다. 전부 action 이면 게임과 128번
# 왕복하므로 이 수는 천장이지 목표가 아니다.
MAX_STATEMENTS = 128
