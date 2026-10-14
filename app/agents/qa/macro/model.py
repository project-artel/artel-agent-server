"""통과한 macro tree 의 모양. 저장되고 다시 읽히는 표현이다.

**텍스트가 원본이고 이 모델은 파생이다.** 텍스트를 저장하는 이유는 `ast.parse` 가
주석과 공백을 버리기 때문이다 — JSON 에서 텍스트를 다시 찍어내면 agent 가 적은 그대로가
아니고, 정의가 바뀌었을 때 사람이 보는 diff 가 실제로 바뀐 줄을 가리키지 않는다. JSON 을
저장하는 이유는 실행할 때마다 다시 파싱하지 않기 위해서다. 저장된 뒤에 허용 목록이
좁아진 정의가 실행 한복판에서 처음 거절되는 것을 막으려면, JSON 은 `register_macro` 가
한 번만 만들고 실행은 저장된 JSON 만 봐야 한다.

그 앞뒤 관계를 코드로 못박는 방법이 둘이다. 하나는 모든 모델이 `frozen` 이고 순서 있는
것이 `tuple` 이라는 것 — 들고 있는 정의를 고치는 길이 없다. 다른 하나는 만드는 길이
`parser.macro_definition_from_source(name, source)` 하나라는 것이다. JSON 만 고치는
경로를 만들지 않는다.

타입은 텍스트에 이미 적혀 있고(`macro-source-parser`) JSON 은 그것을 옮기는 것이지 새로
정하는 것이 아니다. 모델이 타입을 추론하거나 기본값을 채우지 않는다 — tool 호출의
기본값은 `grammar.py` 의 표가 내고, 여기 실리는 것은 저자가 적은 인자뿐이다.
"""

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agents.qa.macro.grammar import MacroOperator, MacroShape, MacroType


class _Frozen(BaseModel):
    """고칠 수 없다는 것이 이 모델들의 계약이다.

    `extra="forbid"` 는 JSON 으로 돌아오는 길을 좁힌다. 손으로 키를 더한 정의가 조용히
    통과해 runner 에 들어가면, 텍스트가 원본이라는 말이 거짓이 된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class MacroLiteral(_Frozen):
    """적힌 리터럴 하나. 모양과 값을 함께 든다.

    `shape` 를 값에서 다시 읽지 않고 싣는 이유는 Python 에서 `True` 가 int 이기도 해서
    다. 값만 보고 되읽으면 bool 리터럴이 number 행으로 빠진다.

    `bool` 을 union 의 맨 앞에 두는 것도 같은 사정이다 — pydantic 이 정확히 맞는 타입을
    먼저 고르므로, `True` 가 `1` 로 좁혀지지 않는다.
    """

    shape: MacroShape
    value: bool | int | float | str


# --- 조준 ---------------------------------------------------------------------


class MacroSelectorTarget(_Frozen):
    """`selector("...")`. 그 자리에서 풀지 않는다.

    문자열에 이름만 붙이고 late binding 을 그대로 둔다. 즉시 풀면 적어 둔 주소인데도
    `STALE_BINDING` 이 나서, 적어 둔 주소면 `REQUIRE_FAILED`, 이 런에서 찾은 값이면
    `STALE_BINDING` 이라는 기준이 깨진다.
    """

    kind: Literal["selector"] = "selector"
    selector: str


class MacroNameTarget(_Frozen):
    """`def` 줄의 parameter 이거나 `find()`·`selector()` 에 bind 된 이름.

    맨이름은 언제나 이 둘 중 하나라는 것이 macro 문법의 불변식이고, 그래서 좌표나 id 를
    적을 문법 자체가 없다.
    """

    kind: Literal["name"] = "name"
    name: str


MacroTarget = Annotated[
    Union[MacroSelectorTarget, MacroNameTarget], Field(discriminator="kind")
]


# --- 문자열 하나가 오는 자리 ----------------------------------------------------


class MacroStringLiteral(_Frozen):
    kind: Literal["literal"] = "literal"
    value: str


class MacroStringName(_Frozen):
    kind: Literal["name"] = "name"
    name: str


MacroStringRef = Annotated[
    Union[MacroStringLiteral, MacroStringName], Field(discriminator="kind")
]


# --- tool 과 helper 호출의 인자 -------------------------------------------------


class MacroTargetArgument(_Frozen):
    kind: Literal["target"] = "target"
    parameter: str
    target: MacroTarget


class MacroLiteralArgument(_Frozen):
    kind: Literal["literal"] = "literal"
    parameter: str
    literal: MacroLiteral


class MacroNameArgument(_Frozen):
    kind: Literal["name"] = "name"
    parameter: str
    name: str
    declared_type: MacroType


MacroArgument = Annotated[
    Union[MacroTargetArgument, MacroLiteralArgument, MacroNameArgument],
    Field(discriminator="kind"),
]


# --- 조건 ---------------------------------------------------------------------


class MacroReaderCall(_Frozen):
    """조건의 왼쪽에 오는 호출 여덟 중 하나."""

    reader: str
    target: MacroTarget | None = None
    key: MacroStringRef | None = None


class MacroLiteralOperand(_Frozen):
    kind: Literal["literal"] = "literal"
    literal: MacroLiteral


class MacroNameOperand(_Frozen):
    """맨이름 하나. 선언 타입을 함께 들어서 저장 시점 검사가 넓어진다.

    `card_a: object` 가 선언돼 있으면 `card_a > 5` 를 평가까지 기다리지 않고 저장 때
    거절한다.
    """

    kind: Literal["name"] = "name"
    name: str
    declared_type: MacroType


class MacroReaderOperand(_Frozen):
    kind: Literal["reader"] = "reader"
    call: MacroReaderCall


class MacroSelectorOperand(_Frozen):
    """`selector(...)` 가 피연산자로 선 자리. 양쪽이 둘 다 object 일 때만 설 수 있다."""

    kind: Literal["selector"] = "selector"
    selector: str


MacroOperand = Annotated[
    Union[
        MacroLiteralOperand,
        MacroNameOperand,
        MacroReaderOperand,
        MacroSelectorOperand,
    ],
    Field(discriminator="kind"),
]


class MacroComparisonCondition(_Frozen):
    kind: Literal["comparison"] = "comparison"
    left: MacroOperand
    operator: MacroOperator
    right: MacroOperand


class MacroReaderCondition(_Frozen):
    """비교 없이 호출 하나뿐인 조건."""

    kind: Literal["call"] = "call"
    call: MacroReaderCall


class MacroPredicateCondition(_Frozen):
    """`-> bool` 인 `def` 하나를 부르는 조건. 인자 모양은 helper 호출과 같다.

    `and`·`or`·`not` 연산자를 열지 않고 조건 둘 이상을 적는 자리가 이것이다. predicate
    몸통의 `if` 가 각각 `return True` 하면 `or` 이고, 어느 `return` 에서 끝났는지가 줄 번호로
    남는다.
    """

    kind: Literal["predicate"] = "predicate"
    callee: str
    arguments: tuple[MacroArgument, ...] = ()


# --- Jev 에게 넘기는 context ----------------------------------------------------
#
# `decide(...)` 와 `ask(...)` 의 keyword 인자 하나가 Jev 요청 `state` 의 key 하나다. key 는
# agent 가 적은 keyword 이름 그대로이고, 값의 모양은 아래 넷 중 하나다. `scene` 은 runner 가
# 언제나 넣으므로 keyword 로 쓸 수 없다.


class MacroContextTarget(_Frozen):
    """`boss=boss` 나 `boss=selector("...")`. 그 object 의 member 값 전부가 들어간다."""

    kind: Literal["target"] = "target"
    key: str
    target: MacroTarget


class MacroContextFindAll(_Frozen):
    """`hand=find_all(name="Card(Clone)")`. 잡힌 object 전부의 목록이 들어간다.

    손패처럼 같은 이름의 object 여럿을 한 번에 보여 줄 자리다. `for` 의 `find_all` 과 같은
    keyword 를 받고, 0 개면 빈 목록이다.
    """

    kind: Literal["find_all"] = "find_all"
    key: str
    find: MacroFindValue


class MacroContextUnder(_Frozen):
    """`hand=under("DebugCanvas[4]/Hand[1]")` 나 `hud=under(panel)`. 그 object 와 그 아래 전부.

    `pulse` 가 들고 있는 live object 중 경로가 `root` 의 경로와 같거나 그 아래인 것 전부의
    목록이 들어간다. `find_all` 과 달리 이름을 몰라도 되므로, 패널 하나를 통째로 보여 줄
    자리다. 경로 문자열은 `MacroSelectorTarget` 으로, object 이름은 `MacroNameTarget` 으로 든다.
    """

    kind: Literal["under"] = "under"
    key: str
    root: MacroTarget


class MacroContextName(_Frozen):
    """`hp=hp`. 앞에서 reader 로 bind 한 값이 bind 된 그대로 들어간다."""

    kind: Literal["name"] = "name"
    key: str
    name: str
    declared_type: MacroType


class MacroContextLiteral(_Frozen):
    """`goal="beat the boss"`. 적힌 리터럴이 그대로 들어간다."""

    kind: Literal["literal"] = "literal"
    key: str
    literal: MacroLiteral


MacroContextArgument = Annotated[
    Union[
        MacroContextTarget,
        MacroContextFindAll,
        MacroContextUnder,
        MacroContextName,
        MacroContextLiteral,
    ],
    Field(discriminator="kind"),
]


class MacroAskCondition(_Frozen):
    """`ask("<question>", key=value, ...)`. Jev 가 참이라고 볼 확률이 0.5 이상이면 참이다.

    `if`·`while`·`require` 의 조건 **전체**로만 선다. 조건 평가는 동기 함수이고 Jev 호출은
    네트워크를 타므로, 그 세 statement 가 평가 전에 따로 기다린다. `wait_until` 과 predicate
    안에서는 저장 때 거절한다 — 앞의 것은 `pulse` 마다 Jev 를 부르게 되고, 뒤의 것은 동기로
    평가된다.
    """

    kind: Literal["ask"] = "ask"
    question: str
    context: tuple[MacroContextArgument, ...] = ()


MacroCondition = Annotated[
    Union[
        MacroComparisonCondition,
        MacroReaderCondition,
        MacroPredicateCondition,
        MacroAskCondition,
    ],
    Field(discriminator="kind"),
]


# --- 대입의 오른쪽 -------------------------------------------------------------


class MacroFindValue(_Frozen):
    """`find(label=..., name=..., under=...)`. keyword 만 받는다."""

    kind: Literal["find"] = "find"
    label: MacroStringRef | None = None
    name: MacroStringRef | None = None
    under: MacroStringRef | None = None


class MacroSelectorValue(_Frozen):
    kind: Literal["selector"] = "selector"
    selector: str


class MacroLiteralValue(_Frozen):
    kind: Literal["literal"] = "literal"
    literal: MacroLiteral


class MacroReadValue(_Frozen):
    """reader 호출 하나. 그 statement 가 도는 순간에 읽고, 그 뒤로 다시 읽지 않는다.

    조작 전에 읽은 값을 조작 뒤의 값과 비교하려고 둔다 — "눌렀더니 숫자가 움직였나"
    가 QA 가 가장 많이 하는 확인이다. 다시 읽으면 그 비교가 성립하지 않는다.
    """

    kind: Literal["read"] = "read"
    call: MacroReaderCall


MacroAssignValue = Annotated[
    Union[MacroFindValue, MacroSelectorValue, MacroLiteralValue, MacroReadValue],
    Field(discriminator="kind"),
]


# --- statement 종류 ---------------------------------------------------------
#
# 한 목록에 섞고 `kind` 로 가른다. 순서가 곧 실행 순서이고, 종류별로 목록을 가르면
# 그 순서를 다시 붙여야 한다 — 그것은 적힌 글에 이미 있는 사실을 두 번째로 적는 일이다.


class _Statement(_Frozen):
    # 저자가 쓴 **원문 그대로**의 그 줄. runner 가 `ctx.run` 의 `summary` 에 넣어
    # `ACTION` frame 마다 그 statement 가 남게 한다.
    #
    # `ast.unparse` 를 쓰지 않는다. 그것은 따옴표를 바꾸고(`"CardSlot/0"` 이
    # `'CardSlot/0'` 이 된다) 주석을 버리므로, timeline 에 남는 것이 agent 가 쓴 글자가
    # 아니게 된다. 이 설계 전체가 텍스트가 원본이고 JSON 이 파생이라는 것 위에 서
    # 있는데, timeline 이 파생에서 역생성한 글을 보여주면 그 원칙이 바로 그 자리에서
    # 깨진다 — macro 를 쓴 agent 가 자기가 쓴 줄을 timeline 에서 못 알아보는 것은
    # 디버깅할 때 제일 비싼 종류의 혼선이다.
    #
    # 여기 싣는 이유는 실행이 저장된 JSON 만 보기 때문이다. 실행 시점에 원문에서 다시
    # 떠내려면 텍스트를 매번 다시 파싱해야 한다.
    source: str


class MacroActionStatement(_Statement):
    kind: Literal["action"] = "action"
    callee: str
    arguments: tuple[MacroArgument, ...] = ()


class MacroHelperCallStatement(_Statement):
    """같은 source 안의 helper `def` 호출. 펼치지 않고 호출로 돈다."""

    kind: Literal["helper"] = "helper"
    callee: str
    arguments: tuple[MacroArgument, ...] = ()


class MacroRequireStatement(_Statement):
    kind: Literal["require"] = "require"
    condition: MacroCondition
    # 조건이 거짓일 때 payload 를 읽는 쪽은 나중의 agent 다. 무엇이 틀렸는지만 있고
    # 어떻게 하라는 말이 없으면 그 agent 가 추측한다. 그래서 필수다.
    remedy: str


class MacroAssignStatement(_Statement):
    kind: Literal["assign"] = "assign"
    name: str
    declared_type: MacroType
    value: MacroAssignValue


class MacroFlagStatement(_Statement):
    kind: Literal["flag"] = "flag"
    message: str


class MacroAskVerdictStatement(_Statement):
    # step 을 들지 않는다. 판정할 step 은 `run_macro` 호출이 받은 것이고 runner 가
    # 그것을 붙인다 — 여기 따로 들면 호출의 step 과 어긋날 자리가 생긴다.
    kind: Literal["ask_verdict"] = "ask_verdict"
    # 언제 써도 참인 문장이다. macro 글은 저작 시점에 고정이라 이번 런에서 본 것을
    # 인용할 수 없으므로, 본 것을 적으면 거짓이 된다.
    expected: str


class MacroIfStatement(_Statement):
    kind: Literal["if"] = "if"
    condition: MacroCondition
    body: tuple["MacroStatement", ...] = ()
    # `elif` 는 여기 중첩된 `if` 하나로 표현된다. Python AST 가 그렇게 읽으므로
    # `if` 를 허용하면 따라온다.
    orelse: tuple["MacroStatement", ...] = ()


class MacroFindAllIterable(_Frozen):
    """`for ... in find_all(...)`. `find` 와 같은 keyword 이고, 여럿을 낸다.

    `for` 에 들어서는 순간 한 번 푼다. 돌면서 다시 풀지 않는다 — 다시 풀면 몸통이 손패를
    바꿀 때마다 도는 대상이 바뀌어, 한 장을 두 번 내거나 한 장도 안 낼 수 있다. 각 회에
    받는 것은 `find` 가 내는 것과 같은 기록이라, 그 사이에 죽었으면 쓰는 자리에서
    `STALE_BINDING` 이 난다.
    """

    kind: Literal["find_all"] = "find_all"
    find: MacroFindValue


class MacroRangeIterable(_Frozen):
    """`for ... in range(n)`. `n` 은 int 리터럴이거나 int 로 선언된 이름이다."""

    kind: Literal["range"] = "range"
    count: MacroLiteral | None = None
    # 리터럴이 아니면 이름. 둘 중 하나만 찬다.
    name: str | None = None


MacroIterable = Annotated[
    Union[MacroFindAllIterable, MacroRangeIterable], Field(discriminator="kind")
]


class MacroForStatement(_Statement):
    """`for <name> in <iterable>:`. 이름의 타입은 도는 대상이 정한다.

    Python 의 `for` 는 이름에 타입을 못 적는다(`for card: object in ...` 은 문법 오류다).
    그래서 `find_all` 이면 `object`, `range` 이면 `int` 로 정해 두고, 그것을 여기 든다.
    """

    kind: Literal["for"] = "for"
    name: str
    declared_type: MacroType
    iterable: MacroIterable
    body: tuple["MacroStatement", ...] = ()


class MacroWhileStatement(_Statement):
    """`while <condition>:`. 조건은 `if` 와 같은 문법이고 매 회 전에 다시 읽는다."""

    kind: Literal["while"] = "while"
    condition: MacroCondition
    body: tuple["MacroStatement", ...] = ()


class MacroWaitUntilStatement(_Statement):
    """`wait_until(<condition>, <seconds>, <remedy>)`. 조건이 참이 될 때까지 기다린다.

    `require` 는 직전 action 뒤에 도착한 `pulse` 하나로 판정하므로, 게임이 몇 초 걸려 다음
    상태로 가는 자리에서는 게임이 틀리지 않았는데도 실패한다. 이것은 새 `pulse` 가 올
    때마다 조건을 다시 읽는다. `seconds` 는 리터럴이고
    `MAX_WAIT_PER_STATEMENT_SECONDS` 를 넘지 않는다. `remedy` 는 `require` 의 것과 같은
    이유로 필수다.
    """

    kind: Literal["wait_until"] = "wait_until"
    condition: MacroCondition
    seconds: float
    remedy: str


class MacroReturnStatement(_Statement):
    """predicate `def` 안에서만 서는 `return`. 둘 중 정확히 하나가 찬다.

    `return True`·`return False` 는 `value` 이고, 그 밖은 조건 하나인 `condition` 이다.
    조건 문법에 bool 리터럴 하나뿐인 조건을 따로 열지 않으려고 둘을 가른다.
    """

    kind: Literal["return"] = "return"
    condition: MacroCondition | None = None
    value: bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "MacroReturnStatement":
        if (self.condition is None) == (self.value is None):
            raise ValueError("a `return` carries exactly one of `condition` and `value`")
        return self


class MacroCheckpointStatement(_Statement):
    """턴을 agent 에게 돌려주는 자리. 실패가 아니다.

    `reason` 은 agent 가 그 자리에서 무엇을 보라는 말이다. 비어 있어도 된다.
    """

    kind: Literal["checkpoint"] = "checkpoint"
    reason: str = ""


class MacroDecideCall(_Frozen):
    """`match` 의 주어인 `decide("<question>", key=value, ...)`."""

    question: str
    context: tuple[MacroContextArgument, ...] = ()


class MacroMatchCase(_Frozen):
    """`case "<imported macro>":` 하나. Jev 가 읽는 선택지 설명은 그 macro 의 docstring 이다."""

    option: str
    body: tuple["MacroStatement", ...] = ()


class MacroMatchStatement(_Statement):
    """`match decide(...):`. Jev 가 고른 `case` 하나의 몸통을 돈다.

    runner 가 선택지에 `NONE_FITS` 를 하나 더 붙인다. Jev 가 그것을 고르면 `checkpoint` 와
    같은 방식으로 멈추고 agent 에게 턴을 돌려준다.
    """

    kind: Literal["match"] = "match"
    decide: MacroDecideCall
    cases: tuple[MacroMatchCase, ...] = ()


MacroStatement = Annotated[
    Union[
        MacroActionStatement,
        MacroHelperCallStatement,
        MacroRequireStatement,
        MacroAssignStatement,
        MacroFlagStatement,
        MacroAskVerdictStatement,
        MacroIfStatement,
        MacroForStatement,
        MacroWhileStatement,
        MacroCheckpointStatement,
        MacroWaitUntilStatement,
        MacroReturnStatement,
        MacroMatchStatement,
    ],
    Field(discriminator="kind"),
]

MacroIfStatement.model_rebuild()
MacroMatchCase.model_rebuild()
MacroMatchStatement.model_rebuild()
MacroForStatement.model_rebuild()
MacroWhileStatement.model_rebuild()


# --- `def` 와 정의 -------------------------------------------------------------


class MacroParameter(_Frozen):
    name: str
    declared_type: MacroType
    # 기본값은 요구가 아니라 허용이다. 기본값을 요구하던 옛 이유 — 비교 오른쪽
    # parameter 의 종류 추론 — 은 타입을 적게 하면서 사라졌다.
    default: MacroLiteral | None = None


class MacroFunction(_Frozen):
    name: str
    parameters: tuple[MacroParameter, ...] = ()
    # 몸통 첫 statement 의 문자열, `inspect.cleandoc` 로 정리한 것. statement 가 아니어서
    # `statements` 에 없고 runner 가 보지 않는다. 기본값 `None` 은 이 field 가 생기기 전에
    # 저장된 JSON 이 `extra="forbid"` 아래에서도 그대로 읽히게 한다.
    docstring: str | None = None
    # `-> None` 이면 "none", predicate 이면 "bool". 기본값은 `docstring` 과 같은 이유로
    # 이 field 가 생기기 전에 저장된 JSON 이 그대로 읽히게 한다.
    returns: Literal["none", "bool"] = "none"
    statements: tuple[MacroStatement, ...] = ()


class MacroDefinition(_Frozen):
    """한 macro 의 전부. 원본 텍스트와 그것에서 나온 `def` 들.

    `entry` 는 macro 이름과 같은 `def` 이고 `helpers` 가 나머지다. 둘을 한 목록에 담고
    이름으로 가리게 두지 않는 이유는, 진입점이 없는 정의가 모델 안에 설 수 있으면
    `run_macro` 가 무엇을 부를지 매번 다시 가려야 하기 때문이다.
    """

    name: str
    # 원본. 이것이 진실이고 아래 둘은 파생이다.
    source: str
    entry: MacroFunction
    helpers: tuple[MacroFunction, ...] = ()
    # `from macros import a, b` 로 가져온 등록된 macro 들. 각자 자기 `entry` 와 `helpers` 를
    # 그대로 들어서, 두 macro 의 helper 이름이 겹쳐도 섞이지 않는다. 저장된 JSON 에도 실리지만
    # 읽는 쪽은 없다 — 읽을 때마다 `source` 를 **지금** 등록된 macro 들로 다시 파싱한다.
    imports: tuple["MacroDefinition", ...] = ()

    def function(self, name: str) -> MacroFunction | None:
        """이 source 에 적힌 `def` 하나. 가져온 macro 는 `imported` 로 찾는다."""
        if name == self.entry.name:
            return self.entry
        for helper in self.helpers:
            if helper.name == name:
                return helper
        return None

    def imported(self, name: str) -> "MacroDefinition | None":
        for definition in self.imports:
            if definition.name == name:
                return definition
        return None


MacroDefinition.model_rebuild()
