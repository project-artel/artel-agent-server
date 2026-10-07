"""macro 텍스트를 `ast.parse` 로 읽고 허용 node 만 통과시킨다.

`eval` 도 `exec` 도 쓰지 않는다. 통과한 tree 를 실행하지도 않는다 — 이 모듈이 내놓는
것은 `model.MacroDefinition` 이고, 그것을 읽어 `JsonRpcAction` 을 조립하는 것은
`runner.py` 다.

**거절 문장은 받을 수 있는 것을 이름으로 전부 댄다.** 그것이 이 파일의 문장 대부분이
긴 이유다. 소문자 `true` 는 `ast.Name` 으로 읽혀 bind 된 적 없는 이름으로 거절되므로,
거절 문장이 `True` 를 이름으로 대야 모델이 한 번에 고친다. 같은 자리가 열 곳이 넘는다.

**언제 검사하나.** 파서는 값을 영영 못 본다. macro 는 저장할 때 파싱되고 값은 돌 때
도착한다. 그래서 두 층이다.

- 비교의 한쪽 모양을 리터럴·선언 타입·읽는 함수의 반환 모양으로 알 수 있으면 여기서
  거절한다.
- 그 밖에는 값이 도착해야 모양을 안다. `static()`·`observable()`·`member()` 가 그렇고,
  그 거절은 `binding.py` 의 몫이다.

**tree 를 한 번만 걷는다.** 검사하면서 모델 node 를 만든다. 검사용 walk 와 변환용 walk
를 따로 두면 허용 node 목록이 두 벌이 되고, 언제나 한쪽만 고쳐진다.
"""

import ast
from dataclasses import dataclass

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import (
    ASK_VERDICT,
    DECLARABLE_TYPE_NAMES,
    DECLARABLE_TYPES,
    FIND,
    FIND_KEYWORDS,
    FIND_REQUIRED_KEYWORDS,
    FLAG,
    FORBIDDEN_TOOLS,
    MAX_CALL_DEPTH,
    MAX_STATEMENTS,
    PAIRED_TOOLS,
    PairedTools,
    READER_NAME_LIST,
    READERS_BY_NAME,
    ReaderSpec,
    REPORTING_NAMES,
    REQUIRE,
    SELECTOR,
    TOOL_NAME_LIST,
    TOOLS_BY_NAME,
    MacroOperator,
    MacroShape,
    MacroType,
    ToolParameter,
    ToolSpec,
    allowed_operators,
    comparison_allowed,
    operator_names,
)
from app.agents.qa.macro.model import (
    MacroActionStatement,
    MacroArgument,
    MacroAskVerdictStatement,
    MacroAssignStatement,
    MacroAssignValue,
    MacroComparisonCondition,
    MacroCondition,
    MacroDefinition,
    MacroFindValue,
    MacroFlagStatement,
    MacroFunction,
    MacroHelperCallStatement,
    MacroIfStatement,
    MacroLiteral,
    MacroLiteralArgument,
    MacroLiteralOperand,
    MacroLiteralValue,
    MacroNameArgument,
    MacroNameOperand,
    MacroNameTarget,
    MacroOperand,
    MacroParameter,
    MacroReaderCall,
    MacroReaderCondition,
    MacroReaderOperand,
    MacroRequireStatement,
    MacroSelectorOperand,
    MacroSelectorTarget,
    MacroSelectorValue,
    MacroStatement,
    MacroStringLiteral,
    MacroStringName,
    MacroStringRef,
    MacroTarget,
    MacroTargetArgument,
)

# `def` 줄과 대입에 적을 수 있는 타입 이름 → enum.
_DECLARABLE_BY_NAME = {declared.value: declared for declared in DECLARABLE_TYPES}
_ALL_TYPE_NAMES = {declared.value for declared in MacroType}

# 선언 타입이 도착할 값의 모양으로 어떻게 떨어지는가. `int` 와 `float` 가 한 모양으로
# 합쳐지는 것이 이 표의 전부다 — wire 가 네 자리로 반올림해 `3.0` 을 `3` 으로 보낸다.
_SHAPE_OF_TYPE: dict[MacroType, MacroShape] = {
    MacroType.int_: MacroShape.number,
    MacroType.float_: MacroShape.number,
    MacroType.string: MacroShape.string,
    MacroType.bool_: MacroShape.bool_,
    MacroType.object_: MacroShape.object_,
    MacroType.vector2: MacroShape.vector2,
    MacroType.vector3: MacroShape.vector3,
}

_COMPARE_OPERATORS: dict[type[ast.cmpop], MacroOperator] = {
    ast.Eq: MacroOperator.equal,
    ast.NotEq: MacroOperator.not_equal,
    ast.Gt: MacroOperator.greater,
    ast.Lt: MacroOperator.less,
    ast.GtE: MacroOperator.greater_or_equal,
    ast.LtE: MacroOperator.less_or_equal,
}

# bind 된 적 없는 이름 중, 무엇을 적으려 했는지가 뻔한 것들. 거절 문장이 고칠 글자를
# 그대로 대 준다.
_MISTYPED_CONSTANTS = {
    "true": "True",
    "false": "False",
    "none": "None",
    "null": "None",
    "nil": "None",
}

# attribute access 의 거절 문장. 읽는 것은 함수가 한다 — 객체의 글자는 `text(t)`,
# 누를 수 있는지는 `actionable(t)`, 있는지는 `exists(t)` 로 읽고 `t.text` 나 `t.id` 는
# 쓸 문법이 없다. 여러 자리에서 같은 말을 해야 하므로 한 번 적는다.
_NO_DOT = (
    "a dot is not allowed anywhere in a macro. Reading is done by a call: "
    '`text(<target>)` for the letters an object shows, `actionable(<target>)` for '
    "whether it can be pressed, `exists(<target>)` and `absent(<target>)` for whether "
    'it is there, and `member(<target>, "<name>")` for a number on it'
)

_STATEMENT_KINDS = (
    "an action call (one of the game tools, or a helper `def` in this same source), "
    "`require(<condition>, <remedy>)`, a typed assignment (`name: object = find(...)`), "
    "`if`, `flag(<message>)`, or `ask_verdict(<step>, <expected>)`"
)


@dataclass(frozen=True)
class _Binding:
    """한 이름이 무엇에 bind 됐나.

    `origin` 을 드는 이유는 target 자리의 거절 문장 하나 때문이다. 문자열 리터럴에
    bind 한 이름을 `click` 에 넣는 것은 좌표 금지를 우회하려는 흔한 시도라, `object` 가
    아니라고만 말하면 무엇이 문제였는지 안 읽힌다.
    """

    declared_type: MacroType
    origin: str


@dataclass(frozen=True)
class _Signature:
    name: str
    parameters: tuple[MacroParameter, ...]


def _written(node: ast.AST, lines: list[str], header_only: bool = False) -> str:
    """그 statement 가 원문에서 차지한 줄, 글자 하나까지 그대로.

    `ast.unparse` 를 쓰지 않는다. 그것은 따옴표를 바꾸고 주석을 버리므로, timeline 에
    남는 것이 저자가 쓴 글자가 아니게 된다. `ast.get_source_segment` 도 모자라다 — node
    의 끝 열에서 멈추므로 줄 끝 주석이 떨어진다. 그래서 줄 자체를 떠낸다.

    들여쓰기는 statement 의 `col_offset` 만큼만 벗긴다. 그만큼이 그 몸통의 들여쓰기이고,
    더 벗기면 여러 줄짜리 호출의 안쪽 정렬이 무너진다.

    `header_only` 는 `if` 다. 몸통까지 떠내면 한 statement 의 텍스트가 블록 전체가
    되는데, 이 값이 쓰이는 자리는 `ACTION` frame 의 한 줄이다.
    """
    first = node.lineno - 1
    last = first if header_only else (node.end_lineno or node.lineno) - 1
    indent = node.col_offset
    taken = []
    for line in lines[first : last + 1]:
        head = line[:indent]
        taken.append(line[indent:] if not head.strip() else line.lstrip())
    return "\n".join(taken).rstrip()


def macro_definition_from_source(name: str, source: str) -> MacroDefinition:
    """macro 텍스트 하나를 통과시켜 정의로 만든다. 못 통과하면 `MacroRejection`.

    정의를 만드는 **유일한** 길이다. JSON 만 고치는 경로를 만들지 않는 것이 이 모듈과
    `model.py` 의 계약이고, 그래서 `MacroDefinition` 은 여기서만 생긴다.
    """
    module = _module(source)
    lines = source.splitlines()
    functions = _top_level_functions(module)
    signatures = _signatures(name, functions)

    entry: MacroFunction | None = None
    helpers: list[MacroFunction] = []
    for node in functions:
        reader = _FunctionReader(signatures, node.name, lines)
        built = MacroFunction(
            name=node.name,
            parameters=signatures[node.name].parameters,
            statements=reader.body(node.body, node),
        )
        if node.name == name:
            entry = built
        else:
            helpers.append(built)

    if entry is None:  # pragma: no cover - `_signatures` 가 이미 거절한다
        raise MacroRejection(
            f"this macro is called {name!r}, so it needs a top-level `def {name}(...)` "
            "to be its entry point."
        )

    definition = MacroDefinition(
        name=name, source=source, entry=entry, helpers=tuple(helpers)
    )
    # 네 검사가 같은 호출 그래프에 묻는다. 그래프를 네 번 다시 세우고 memo 를 네 번
    # 손으로 달면, 빠뜨린 하나가 지수로 터진다.
    graph = _CallGraph(definition)
    graph.reject_recursion()
    graph.reject_over_depth()
    graph.reject_over_statements()
    graph.reject_unpaired()
    return definition


# --- module 과 `def` -----------------------------------------------------------


def _module(source: str) -> ast.Module:
    if not source.strip():
        raise MacroRejection(
            "A macro source cannot be empty. Write one `def` named after the macro, "
            f"whose parameters and return are typed: {DECLARABLE_TYPE_NAMES} for a "
            "parameter, and `None` for the return."
        )
    try:
        return ast.parse(source)
    except SyntaxError as error:
        raise MacroRejection(
            f"this is not valid Python syntax — {error.msg}.{_syntax_hint(error.msg)}",
            error.lineno,
        ) from error


def _syntax_hint(message: str) -> str:
    """Python 의 문장만으로는 무엇을 고치면 되는지 안 읽히는 한 경우.

    `require(hp = 100, "...")` 가 그것이다. Python 은 `hp = 100` 을 keyword 인자로
    읽으므로 뒤에 온 remedy 가 "positional argument follows keyword argument" 가 되고,
    그 문장은 비교를 적으려던 저자에게 아무것도 말해 주지 않는다. `require(hp = 100)`
    처럼 remedy 가 없으면 문법 오류조차 아니어서 `_require` 가 같은 말을 한다.
    """
    if "keyword argument" in message:
        return (
            " Inside a call, `name = value` is a keyword argument rather than a "
            "comparison. To ask whether two things are equal, write `==`."
        )
    return ""


def _top_level_functions(module: ast.Module) -> list[ast.FunctionDef]:
    functions: list[ast.FunctionDef] = []
    for node in module.body:
        if isinstance(node, ast.FunctionDef):
            functions.append(node)
            continue
        if isinstance(node, ast.AsyncFunctionDef):
            raise MacroRejection(
                "`async def` is not allowed. Write a plain `def`.", node.lineno
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise MacroRejection(
                "`import` is not allowed anywhere in a macro. Everything a macro can "
                f"call is already named for it: the game tools ({TOOL_NAME_LIST}), the "
                f"readers ({READER_NAME_LIST}), `find()`, `selector()`, `require()`, "
                "`flag()` and `ask_verdict()`.",
                node.lineno,
            )
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            raise MacroRejection(
                "a bare string at the top of a macro reads as a docstring and is not "
                "allowed. Put the explanation in a `#` comment — the stored source "
                "keeps comments, so nothing is lost.",
                node.lineno,
            )
        raise MacroRejection(
            "only `def` is allowed at the top of a macro source. The `def` named after "
            "the macro is its entry point and every other `def` is a helper it may call.",
            node.lineno,
        )
    if not functions:
        raise MacroRejection(
            "A macro source needs at least one `def`: the one named after the macro."
        )
    return functions


def _signatures(name: str, functions: list[ast.FunctionDef]) -> dict[str, _Signature]:
    signatures: dict[str, _Signature] = {}
    for node in functions:
        if node.name in signatures:
            raise MacroRejection(
                f"`def {node.name}` is written twice. One name is one function, so the "
                "second cannot be reached at all.",
                node.lineno,
            )
        if node.decorator_list:
            raise MacroRejection(
                "a decorator is not allowed on a macro `def`.", node.lineno
            )
        signatures[node.name] = _Signature(node.name, _parameters(node))

    if name not in signatures:
        written = ", ".join(sorted(signatures))
        raise MacroRejection(
            f"this macro is called {name!r}, so it needs a top-level `def {name}(...)` "
            f"to be its entry point. The source defines: {written}."
        )
    return signatures


def _parameters(node: ast.FunctionDef) -> tuple[MacroParameter, ...]:
    arguments = node.args
    if arguments.posonlyargs:
        raise MacroRejection(
            "positional-only parameters (`/`) are not allowed on a macro `def`.",
            node.lineno,
        )
    if arguments.kwonlyargs:
        raise MacroRejection(
            "keyword-only parameters (`*`) are not allowed on a macro `def`.", node.lineno
        )
    if arguments.vararg is not None or arguments.kwarg is not None:
        raise MacroRejection(
            "`*args` and `**kwargs` are not allowed on a macro `def`. Name every "
            "parameter and type it.",
            node.lineno,
        )

    if node.returns is None:
        raise MacroRejection(
            f"`def {node.name}` has no return type. A macro returns no value, so write "
            "`-> None`.",
            node.lineno,
        )
    if not (isinstance(node.returns, ast.Constant) and node.returns.value is None):
        raise MacroRejection(
            f"`def {node.name}` must be declared `-> None`. A macro returns no value — "
            "`return` is refused inside it — so `None` is the only return type there is.",
            node.lineno,
        )

    # 기본값은 `args` 의 뒤쪽에 붙는다. 앞에서부터 세면 어느 parameter 의 것인지가
    # 어긋나므로 뒤에서 맞춘다.
    defaults: list[ast.expr | None] = [None] * (
        len(arguments.args) - len(arguments.defaults)
    )
    defaults.extend(arguments.defaults)

    parameters: list[MacroParameter] = []
    seen: set[str] = set()
    for argument, default in zip(arguments.args, defaults):
        if argument.arg in seen:  # pragma: no cover - `ast.parse` 가 이미 거절한다
            raise MacroRejection(
                f"`{argument.arg}` is named twice in `def {node.name}`.", node.lineno
            )
        seen.add(argument.arg)
        declared = _declared_type(argument.annotation, f"parameter `{argument.arg}`", node)
        literal = None
        if default is not None:
            literal = _literal(default)
            if literal is None:
                raise MacroRejection(
                    f"the default for `{argument.arg}` must be one literal — an int, a "
                    "float, a string or a bool. A call, a name or an expression cannot "
                    "be a default, because a macro's defaults are read when it is "
                    "stored, not when it runs.",
                    node.lineno,
                )
            _reject_type_mismatch(
                declared, literal, f"the default for `{argument.arg}`", node
            )
        parameters.append(
            MacroParameter(name=argument.arg, declared_type=declared, default=literal)
        )
    return tuple(parameters)


def _declared_type(
    annotation: ast.expr | None, what: str, node: ast.AST
) -> MacroType:
    if annotation is None:
        raise MacroRejection(
            f"{what} has no type. Every parameter and every assignment in a macro is "
            f"typed, and the type is one of: {DECLARABLE_TYPE_NAMES}.",
            getattr(node, "lineno", None),
        )
    if not isinstance(annotation, ast.Name):
        raise MacroRejection(
            f"the type of {what} is not a plain type name. It is one of: "
            f"{DECLARABLE_TYPE_NAMES}.",
            annotation.lineno,
        )
    if annotation.id in _DECLARABLE_BY_NAME:
        return _DECLARABLE_BY_NAME[annotation.id]
    if annotation.id in _ALL_TYPE_NAMES:
        # `vector2` 와 `vector3` 다. v1 에 vector 를 만들 문법도 비교할 연산자도 없어서
        # 선언해 봐야 할 수 있는 일이 없다. 두 이름은 도착한 값의 모양으로만 산다.
        raise MacroRejection(
            f"{what} is declared `{annotation.id}`, which cannot be declared: a macro "
            "has no way to build a vector and no operator that compares one, so a "
            f"vector-typed name could do nothing. The types you can declare are: "
            f"{DECLARABLE_TYPE_NAMES}.",
            annotation.lineno,
        )
    raise MacroRejection(
        f"`{annotation.id}` is not a macro type. {what} takes one of: "
        f"{DECLARABLE_TYPE_NAMES}.",
        annotation.lineno,
    )


# --- 리터럴 --------------------------------------------------------------------


def _literal(node: ast.expr) -> MacroLiteral | None:
    """리터럴 하나면 그것을, 아니면 `None`.

    `-1` 은 `ast.Constant` 가 아니라 `UnaryOp(USub, Constant(1))` 이다. 숫자 앞 부호
    하나만 열어야 `set_input_axis("Horizontal", -1)` 같은 호출이 써진다. 그 밖의
    연산자는 전부 거절이고, 거절은 부르는 쪽이 한다.

    bool 을 먼저 가른다. Python 에서 `True` 는 int 이기도 하므로 number 행으로 빠지면
    `(bool, >)` 거절 칸이 집행되지 않는다.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _literal(node.operand)
        if inner is None or inner.shape is not MacroShape.number:
            return None
        return MacroLiteral(shape=MacroShape.number, value=-inner.value)
    if not isinstance(node, ast.Constant):
        return None
    value = node.value
    if isinstance(value, bool):
        return MacroLiteral(shape=MacroShape.bool_, value=value)
    if isinstance(value, (int, float)):
        return MacroLiteral(shape=MacroShape.number, value=value)
    if isinstance(value, str):
        return MacroLiteral(shape=MacroShape.string, value=value)
    return None


def _reject_type_mismatch(
    declared: MacroType, literal: MacroLiteral, what: str, node: ast.AST
) -> None:
    """선언 타입과 리터럴이 안 맞으면 거절한다.

    예외는 하나다. int 리터럴을 `float` 선언에 넣는 것(`speed: float = 3`)은 된다.
    반대(`count: int = 3.5`)는 거절한다. wire 가 `3.0` 을 `3` 으로 보내는 마당에
    `3.0` 을 요구하면 저자를 괴롭히기만 한다.
    """
    line = getattr(node, "lineno", None)
    if declared is MacroType.object_:
        raise MacroRejection(
            f"{what} is declared `object` but holds a literal. An `object` name holds a "
            "pulse record, so its value comes from `find(...)` or `selector(...)`.",
            line,
        )
    if declared is MacroType.int_:
        # `shape` 를 먼저 본다. Python 에서 `True` 는 int 이기도 하므로 값의
        # `isinstance` 만 보면 `count: int = True` 가 통과한다 — `MacroLiteral` 이
        # 모양을 따로 싣는 이유가 이것이다.
        if literal.shape is MacroShape.number and isinstance(literal.value, int):
            return
        raise MacroRejection(
            f"{what} is declared `int` but holds {literal.value!r}, which is a "
            f"{literal.shape.value}. Declare it `float` if it can have a fraction; an "
            "int literal goes into a `float` but not the other way round.",
            line,
        )
    if declared is MacroType.float_:
        if literal.shape is MacroShape.number:
            return
        raise MacroRejection(
            f"{what} is declared `float` but holds {literal.value!r}.", line
        )
    if _SHAPE_OF_TYPE[declared] is literal.shape:
        return
    raise MacroRejection(
        f"{what} is declared `{declared.value}` but holds {literal.value!r}, which is a "
        f"{literal.shape.value}. The types you can declare are: {DECLARABLE_TYPE_NAMES}.",
        line,
    )


def _string_literal(node: ast.expr, what: str) -> str:
    literal = _literal(node)
    if literal is None or literal.shape is not MacroShape.string:
        raise MacroRejection(
            f"{what} must be a plain string written out in the macro, in quotes.",
            getattr(node, "lineno", None),
        )
    return literal.value


# --- 한 `def` 의 몸통 ----------------------------------------------------------


class _FunctionReader:
    """`def` 하나를 읽는다. 이름의 bind 를 세는 자리가 여기다.

    이름을 bind 하는 규칙은 `def` 하나 안에서 센다. 한 `def` 안에서 맨이름 하나는 한 가지만
    뜻해야 하므로, 같은 몸통 안의 재대입도 안쪽 몸통이 바깥 이름을 다시 bind 하는 것도
    거절이다. 형제 분기(`if` 와 `else`)가 같은 이름을 bind 하는 것은 된다 — 두 몸통은 겹치지
    않고 어느 쪽도 밖으로 안 나간다.
    """

    def __init__(
        self, signatures: dict[str, _Signature], name: str, lines: list[str]
    ) -> None:
        self.signatures = signatures
        self.name = name
        # 원문의 줄. statement 마다 저자가 쓴 그 줄을 그대로 떠내는 데 쓴다.
        self.lines = lines
        self.parameters: dict[str, _Binding] = {
            parameter.name: _Binding(parameter.declared_type, "parameter")
            for parameter in signatures[name].parameters
        }
        # 몸통마다 한 칸. `if` 와 `else` 는 각자 자기 칸을 받고 끝나면 버려진다.
        self.scopes: list[dict[str, _Binding]] = []

    # -- 이름 --

    def _lookup(self, name: str) -> _Binding | None:
        for scope in reversed(self.scopes):
            if name in scope:
                return scope[name]
        return self.parameters.get(name)

    def _require_bound(self, node: ast.Name) -> _Binding:
        found = self._lookup(node.id)
        if found is not None:
            return found
        if node.id in _MISTYPED_CONSTANTS:
            raise MacroRejection(
                f"`{node.id}` is read as a name, not a value. Write "
                f"`{_MISTYPED_CONSTANTS[node.id]}`.",
                node.lineno,
            )
        if node.id in TOOLS_BY_NAME or node.id in READERS_BY_NAME:
            raise MacroRejection(
                f"`{node.id}` is a call, not a value. Write `{node.id}(...)`.", node.lineno
            )
        bound = ", ".join(sorted(self.parameters) + sorted(self._visible())) or "none yet"
        raise MacroRejection(
            f"`{node.id}` is not bound in `def {self.name}`. A bare name is always "
            "either a parameter on the `def` line or a name bound by `find(...)` or "
            f"`selector(...)` earlier in this same body. Bound here: {bound}.",
            node.lineno,
        )

    def _visible(self) -> set[str]:
        names: set[str] = set()
        for scope in self.scopes:
            names |= set(scope)
        return names

    def _bind(self, name: str, declared: MacroType, origin: str, node: ast.AST) -> None:
        line = getattr(node, "lineno", None)
        if name in self.parameters:
            raise MacroRejection(
                f"`{name}` is already a parameter of `def {self.name}`, so binding it "
                "again would hide it. One bare name means one thing inside one `def`.",
                line,
            )
        for scope in self.scopes:
            if name in scope:
                raise MacroRejection(
                    f"`{name}` is already bound in `def {self.name}`. A macro has no "
                    "re-assignment: one bare name means one thing inside one `def`, so "
                    "pick another name.",
                    line,
                )
        self.scopes[-1][name] = _Binding(declared, origin)

    # -- 몸통 --

    def body(
        self, nodes: list[ast.stmt], owner: ast.AST
    ) -> tuple[MacroStatement, ...]:
        if not nodes:
            raise MacroRejection(
                "an empty body is not allowed. A body holds at least one statement: "
                f"{_STATEMENT_KINDS}.",
                getattr(owner, "lineno", None),
            )
        self.scopes.append({})
        try:
            return tuple(self._statement(node) for node in nodes)
        finally:
            # 몸통이 끝나면 그 안에서 bind 한 이름도 끝난다. 밖에서 쓰면 bind 되지 않은
            # 이름이 되므로, 버리는 것 자체가 그 거절이다.
            self.scopes.pop()

    def _statement(self, node: ast.stmt) -> MacroStatement:
        if isinstance(node, ast.AnnAssign):
            return self._assignment(node)
        if isinstance(node, ast.If):
            return self._conditional(node)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return self._call_statement(node.value)
        self._reject_statement(node)

    def _reject_statement(self, node: ast.stmt) -> None:
        line = node.lineno
        if isinstance(node, ast.Assign):
            raise MacroRejection(
                "every assignment in a macro is typed. Write "
                "`name: <type> = <value>`, where the type is one of: "
                f"{DECLARABLE_TYPE_NAMES}.",
                line,
            )
        if isinstance(node, (ast.AugAssign,)):
            raise MacroRejection(
                "`+=` and its kin are not allowed: a macro has no re-assignment, so a "
                "name's value never changes after it is bound.",
                line,
            )
        if isinstance(node, (ast.For, ast.AsyncFor)):
            raise MacroRejection(
                "`for` is not allowed. How many times it would turn is unknown when the "
                "macro is stored, so the statement total would be unknown too — and that "
                "total is what `step N of M` and the 128-statement limit are counted "
                "from. Write the statements out, or use `if`.",
                line,
            )
        if isinstance(node, ast.While):
            raise MacroRejection(
                "`while` is not allowed, for the same reason `for` is not: how many "
                "times it would turn is unknown when the macro is stored, so the "
                "statement total would be unknown too. Write the statements out, or use "
                "`if`.",
                line,
            )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise MacroRejection(
                "a nested `def` is not allowed. Every helper sits at the top of the "
                "source, beside the entry point.",
                line,
            )
        if isinstance(node, ast.Return):
            raise MacroRejection(
                "`return` is not allowed: a macro and its helpers return no value, which "
                "is why every `def` is declared `-> None`. A helper acts; it does not "
                "hand a value back.",
                line,
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise MacroRejection("`import` is not allowed anywhere in a macro.", line)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            raise MacroRejection(
                "a bare string is read as a docstring and is not allowed. Put the "
                "explanation in a `#` comment — the stored source keeps comments.",
                line,
            )
        if isinstance(node, ast.AnnAssign):  # pragma: no cover - 위에서 이미 다룬다
            raise MacroRejection("this assignment is not allowed.", line)
        raise MacroRejection(
            f"this is not one of the statements a macro allows: {_STATEMENT_KINDS}.",
            line,
        )

    # -- 대입 --

    def _assignment(self, node: ast.AnnAssign) -> MacroAssignStatement:
        if not isinstance(node.target, ast.Name):
            raise MacroRejection(
                "an assignment binds one bare name. Writing to an attribute or an index "
                "is not allowed — a macro reads an object with `text()`, `member()`, "
                "`actionable()`, `exists()` and `absent()`, never with a dot.",
                node.lineno,
            )
        if node.value is None:
            raise MacroRejection(
                f"`{node.target.id}` is declared with no value. Every assignment binds a "
                "name to something: `find(...)`, `selector(...)`, or one literal.",
                node.lineno,
            )
        declared = _declared_type(node.annotation, f"`{node.target.id}`", node)
        value, origin = self._assigned_value(node.value, declared, node.target.id)
        # bind 는 오른쪽을 읽은 뒤에 한다. `a: object = find(name=a)` 가 자기 자신을 보지
        # 않게 하려는 것이다.
        self._bind(node.target.id, declared, origin, node)
        return MacroAssignStatement(
            source=_written(node, self.lines),
            name=node.target.id,
            declared_type=declared,
            value=value,
        )

    def _assigned_value(
        self, node: ast.expr, declared: MacroType, name: str
    ) -> tuple[MacroAssignValue, str]:
        literal = _literal(node)
        if literal is not None:
            _reject_type_mismatch(declared, literal, f"`{name}`", node)
            return MacroLiteralValue(literal=literal), "literal"

        if isinstance(node, ast.Name):
            # 먼저 bind 됐는지 묻는다. 소문자 `true` 가 여기로 오므로, 그 자리에서 `True` 를
            # 이름으로 대 줘야 모델이 한 번에 고친다. bind 된 이름이면 아래에서 거절한다 —
            # 이름을 다시 bind 하는 것은 재대입과 같은 것이고 macro 에 재대입은 없다.
            self._require_bound(node)
            raise MacroRejection(
                f"`{name}` is bound to `{node.id}`, which is already a name in this "
                "`def`. A macro binds a name to `find(...)`, `selector(...)`, or one "
                f"literal — never to another name. Use `{node.id}` where you meant it.",
                node.lineno,
            )

        if isinstance(node, ast.Attribute):
            raise MacroRejection(f"{_NO_DOT}.", node.lineno)

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == FIND:
                self._require_object(declared, name, FIND, node)
                return self._find(node), FIND
            if node.func.id == SELECTOR:
                self._require_object(declared, name, SELECTOR, node)
                return (
                    MacroSelectorValue(selector=self._selector_string(node)),
                    SELECTOR,
                )

        raise MacroRejection(
            f"`{name}` is bound to something a macro cannot bind. The right-hand side is "
            "one of three things: `find(...)`, `selector(...)`, or one literal (an int, "
            "a float, a string or a bool). Arithmetic and any other call are refused.",
            getattr(node, "lineno", None),
        )

    def _require_object(
        self, declared: MacroType, name: str, called: str, node: ast.AST
    ) -> None:
        if declared is MacroType.object_:
            return
        raise MacroRejection(
            f"`{name}` is bound to `{called}(...)` but declared "
            f"`{declared.value}`. Both calls hand back a pulse record, so the name that "
            "holds one is declared `object`.",
            getattr(node, "lineno", None),
        )

    def _find(self, node: ast.Call) -> MacroFindValue:
        if node.args:
            raise MacroRejection(
                "`find` takes keyword arguments only: "
                f"{', '.join(f'{keyword}=' for keyword in FIND_KEYWORDS)}. `label=` "
                "matches the text an object is showing on screen and `name=` matches the "
                "last segment of its selector, so a bare argument would not say which "
                "you meant.",
                node.lineno,
            )
        given: dict[str, MacroStringRef] = {}
        for keyword in node.keywords:
            if keyword.arg is None:
                raise MacroRejection(
                    "`**` is not allowed in a `find` call.", node.lineno
                )
            if keyword.arg not in FIND_KEYWORDS:
                raise MacroRejection(
                    f"`find` has no `{keyword.arg}=` argument. It takes "
                    f"{', '.join(f'{name}=' for name in FIND_KEYWORDS)}.",
                    node.lineno,
                )
            if keyword.arg in given:
                raise MacroRejection(
                    f"`{keyword.arg}=` is given twice in this `find` call.", node.lineno
                )
            given[keyword.arg] = self._string_ref(
                keyword.value, f"`{keyword.arg}=` in `find`"
            )

        if not any(keyword in given for keyword in FIND_REQUIRED_KEYWORDS):
            raise MacroRejection(
                "`find` needs at least one of `label=` or `name=`. `under=` only narrows "
                "the search; on its own it names nothing.",
                node.lineno,
            )
        return MacroFindValue(
            label=given.get("label"), name=given.get("name"), under=given.get("under")
        )

    def _selector_string(self, node: ast.Call) -> str:
        if node.keywords or len(node.args) != 1:
            raise MacroRejection(
                "`selector` takes exactly one plain string: the Unity hierarchy path, "
                'e.g. selector("Root[0]/Canvas[1]/Card(Clone)[3]").',
                node.lineno,
            )
        return _string_literal(node.args[0], "the argument of `selector`")

    def _string_ref(self, node: ast.expr, what: str) -> MacroStringRef:
        """문자열 하나가 오는 자리. 리터럴이거나 `string` 으로 선언된 이름이다."""
        literal = _literal(node)
        if literal is not None:
            if literal.shape is not MacroShape.string:
                raise MacroRejection(
                    f"{what} must be a string, not {literal.value!r}.", node.lineno
                )
            return MacroStringLiteral(value=literal.value)
        if isinstance(node, ast.Name):
            bound = self._require_bound(node)
            if bound.declared_type is not MacroType.string:
                raise MacroRejection(
                    f"{what} is `{node.id}`, which is declared "
                    f"`{bound.declared_type.value}`. That slot takes text, so the name "
                    "has to be declared `string`.",
                    node.lineno,
                )
            return MacroStringName(name=node.id)
        raise MacroRejection(
            f"{what} must be a plain string in quotes, or a name declared `string`.",
            getattr(node, "lineno", None),
        )

    # -- `if` --

    def _conditional(self, node: ast.If) -> MacroIfStatement:
        condition = self._condition(node.test)
        body = self.body(node.body, node)
        # `else` 가 없으면 `orelse` 가 비고, `elif` 는 여기 중첩된 `If` 하나로 온다.
        # Python AST 가 `elif` 를 그렇게 표현하므로 `If` 를 허용하면 따라온다.
        orelse = self.body(node.orelse, node) if node.orelse else ()
        return MacroIfStatement(
            source=_written(node, self.lines, header_only=True),
            condition=condition,
            body=body,
            orelse=orelse,
        )

    # -- 조건 --

    def _condition(self, node: ast.expr) -> MacroCondition:
        """`require` 의 첫 인자와 `if` 의 조건이 같은 문법을 쓴다.

        평가기가 하나이기 때문이다. 그래서 `and`·`or`·`not` 은 `if` 의 조건에서도
        거절이고, 평가 시점의 연산자 거절도 한 곳에서 난다.
        """
        if isinstance(node, ast.BoolOp):
            joiner = "and" if isinstance(node.op, ast.And) else "or"
            raise MacroRejection(
                f"`{joiner}` is not allowed in a condition. One condition is one "
                "comparison or one reader call. Write two `require` calls instead — "
                "each one then says on its own what to do when it fails.",
                node.lineno,
            )
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            raise MacroRejection(
                "`not` is not allowed in a condition. Turn the comparison around: `!=` "
                "for `not ... ==`, and `absent(<target>)` for `not exists(<target>)`.",
                node.lineno,
            )
        if isinstance(node, ast.Compare):
            return self._comparison(node)
        if isinstance(node, ast.Name):
            # 맨이름 하나는 조건이 아니다. 그래도 먼저 bind 됐는지 묻는다 — 소문자 `true`
            # 가 `ast.Name` 으로 읽히므로, 그 자리에서 `True` 를 이름으로 대 줘야
            # 모델이 한 번에 고친다.
            bound = self._require_bound(node)
            raise MacroRejection(
                f"`{node.id}` is a {bound.declared_type.value}, not a condition. Compare "
                "it: `== `, `!=`, `>`, `<`, `>=` or `<=` against a literal or another "
                "name.",
                node.lineno,
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in READERS_BY_NAME:
                spec = READERS_BY_NAME[node.func.id]
                # 비교 없는 조건은 참거짓으로 도착해야 한다. 그 밖의 모양을 진리값으로
                # 접으면 빈 문자열이 거짓, 아무 사전이 참이 되어 조용히 틀린다 —
                # `require` 가 게임 결함처럼 실패하는 바로 그 경우다. 모양을 저장
                # 시점에 아는 다섯은 여기서 거절하고, 모르는 셋(`static`·`observable`·
                # `member`)은 값이 도착한 뒤 `binding.evaluate` 가 같은 거절을 한다.
                if spec.shape is not None and spec.shape is not MacroShape.bool_:
                    raise MacroRejection(
                        f"`{spec.name}(...)` reads a {spec.shape.value}, and a condition "
                        "written without a comparison has to be a bool. Compare it: "
                        f"`{spec.name}(...) == <value>`. The readers that are already a "
                        "bool are: actionable(), exists(), absent().",
                        node.lineno,
                    )
                return MacroReaderCondition(call=self._reader(node))
            raise MacroRejection(
                f"`{node.func.id}` cannot be a condition. The calls a condition reads "
                f"with are: {READER_NAME_LIST}.",
                node.lineno,
            )
        raise MacroRejection(
            "a condition is one comparison (`==`, `!=`, `>`, `<`, `>=`, `<=`) or one "
            f"reader call ({READER_NAME_LIST}).",
            getattr(node, "lineno", None),
        )

    def _comparison(self, node: ast.Compare) -> MacroComparisonCondition:
        if len(node.ops) != 1 or len(node.comparators) != 1:
            raise MacroRejection(
                "a chained comparison (`a < b < c`) is not allowed. One condition is one "
                "comparison; write two `require` calls.",
                node.lineno,
            )
        operator = _COMPARE_OPERATORS.get(type(node.ops[0]))
        if operator is None:
            raise MacroRejection(
                "that comparison operator is not allowed. A macro compares with `==`, "
                "`!=`, `>`, `<`, `>=` or `<=`.",
                node.lineno,
            )

        left, left_shape = self._operand(node.left, "the left side")
        right, right_shape = self._operand(node.comparators[0], "the right side")
        self._reject_comparison(
            operator, left, left_shape, right, right_shape, node
        )
        return MacroComparisonCondition(left=left, operator=operator, right=right)

    def _reject_comparison(
        self,
        operator: MacroOperator,
        left: MacroOperand,
        left_shape: MacroShape | None,
        right: MacroOperand,
        right_shape: MacroShape | None,
        node: ast.AST,
    ) -> None:
        line = getattr(node, "lineno", None)

        # `selector(...)` 는 값을 읽는 reader 가 아니라 객체를 지목하는 표현이다. 양쪽이
        # 둘 다 object 일 때만 피연산자로 설 수 있고, 그 사실은 저장 시점에 알 수 있다.
        for operand, other_shape in ((left, right_shape), (right, left_shape)):
            if not isinstance(operand, MacroSelectorOperand):
                continue
            if other_shape is MacroShape.object_:
                continue
            named = "unknown until the macro runs" if other_shape is None else other_shape.value
            raise MacroRejection(
                "`selector(...)` can only be compared with an `object` — a parameter or "
                "a name declared `object`, or another `selector(...)`. The other side "
                f"here is {named}.",
                line,
            )

        if (
            left_shape is not None
            and right_shape is not None
            and left_shape is not right_shape
        ):
            # 그냥 두면 영원히 거짓인 `require` 가 서고, 저자는 게임이 잘못된 줄 안다.
            # 조용히 틀리는 것이 에러보다 나쁘다.
            raise MacroRejection(
                f"the two sides of this comparison are different shapes: "
                f"{left_shape.value} on the left and {right_shape.value} on the right. "
                "A comparison across shapes would be false forever, which reads as a "
                "broken game rather than a broken macro. `int` and `float` are the one "
                "exception — both arrive as number.",
                line,
            )

        for shape in (left_shape, right_shape):
            if shape is None or comparison_allowed(shape, operator):
                continue
            raise MacroRejection(
                f"`{operator.value}` is not allowed on a {shape.value}. On a "
                f"{shape.value} a macro can use: {operator_names(allowed_operators(shape))}.",
                line,
            )

    def _operand(
        self, node: ast.expr, side: str
    ) -> tuple[MacroOperand, MacroShape | None]:
        literal = _literal(node)
        if literal is not None:
            return MacroLiteralOperand(literal=literal), literal.shape

        if isinstance(node, ast.Name):
            bound = self._require_bound(node)
            return (
                MacroNameOperand(name=node.id, declared_type=bound.declared_type),
                _SHAPE_OF_TYPE[bound.declared_type],
            )

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in READERS_BY_NAME:
                call = self._reader(node)
                return (
                    MacroReaderOperand(call=call),
                    READERS_BY_NAME[node.func.id].shape,
                )
            if node.func.id == SELECTOR:
                return (
                    MacroSelectorOperand(selector=self._selector_string(node)),
                    MacroShape.object_,
                )
            if node.func.id == FIND:
                raise MacroRejection(
                    "`find(...)` cannot stand in a comparison. Bind it to a name first "
                    "(`card: object = find(label=\"...\")`) and compare the name — that "
                    "way a lookup that finds nothing is reported where it happened.",
                    node.lineno,
                )
            raise MacroRejection(
                f"`{node.func.id}` cannot stand on {side} of a comparison. The calls a "
                f"condition reads with are: {READER_NAME_LIST}, and `selector(...)` may "
                "stand against another `object`.",
                node.lineno,
            )

        if isinstance(node, ast.Attribute):
            raise MacroRejection(
                f"{_NO_DOT}.", node.lineno
            )
        if isinstance(node, ast.JoinedStr):
            raise MacroRejection(
                "an f-string is not allowed. Write the text out, or compare against a "
                "name declared `string`.",
                node.lineno,
            )
        if isinstance(node, ast.BinOp):
            raise MacroRejection(
                "arithmetic is not allowed in a condition. Compare a read value against "
                "a literal, and write the number out.",
                node.lineno,
            )
        raise MacroRejection(
            f"{side} of this comparison is not something a macro can compare. Each side "
            f"is one literal, one bare name, one reader call ({READER_NAME_LIST}), or "
            "`selector(...)` against another `object`.",
            getattr(node, "lineno", None),
        )

    def _reader(self, node: ast.Call) -> MacroReaderCall:
        spec = READERS_BY_NAME[node.func.id]  # type: ignore[union-attr]
        if node.keywords:
            raise MacroRejection(
                f"`{spec.name}` takes no keyword arguments.", node.lineno
            )

        wanted = int(spec.takes_target) + int(spec.takes_key)
        if len(node.args) != wanted:
            raise MacroRejection(
                f"`{spec.name}` takes {_reader_arguments(spec)}.", node.lineno
            )

        index = 0
        target: MacroTarget | None = None
        if spec.takes_target:
            target = self._target(node.args[index], f"the target of `{spec.name}`")
            index += 1
        key: MacroStringRef | None = None
        if spec.takes_key:
            key = self._string_ref(node.args[index], f"the name read by `{spec.name}`")
        return MacroReaderCall(reader=spec.name, target=target, key=key)

    # -- 조준 --

    def _target(self, node: ast.expr, what: str) -> MacroTarget:
        """target 자리에 올 수 있는 셋만 통과시킨다.

        `selector(<문자열 리터럴>)`, `def` 줄의 parameter, `find()` 나 `selector()` 에
        bind 된 이름. 맨문자열 좌표(`640,360`)나 id(`#12345`)를 적을 문법 자체가 없다.
        """
        if isinstance(node, ast.Attribute):
            # `click(t.id)` 가 이 자리다. id 를 꺼내 쓰는 것이 조준의 자연스러운 모양처럼
            # 보이므로, 거절 문장이 읽는 함수를 이름으로 대 줘야 한다.
            raise MacroRejection(f"{_NO_DOT}.", node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == SELECTOR:
                return MacroSelectorTarget(selector=self._selector_string(node))
            if node.func.id == FIND:
                raise MacroRejection(
                    f"{what} cannot be `find(...)` written inline. Bind it to a name "
                    "first, so a lookup that finds nothing is reported at the assignment "
                    "rather than in the middle of an action.",
                    node.lineno,
                )
            raise MacroRejection(
                f"{what} cannot be a call. It is `selector(\"<path>\")`, a parameter on "
                "the `def` line, or a name bound by `find(...)` or `selector(...)`.",
                node.lineno,
            )

        if isinstance(node, ast.Name):
            bound = self._require_bound(node)
            if bound.origin == "literal" and bound.declared_type is MacroType.string:
                # `t: string = "640,360"` 을 bind 해 `click(t)` 하면 좌표가 target 자리로
                # 들어가 macro 의 좌표 금지를 우회한다.
                raise MacroRejection(
                    f"{what} is `{node.id}`, which holds a string this macro wrote out. "
                    "A target is never written out as text — that is how a coordinate or "
                    "a `#id` would get in. Aim with `selector(\"<path>\")`, a parameter, "
                    "or a name bound by `find(...)`.",
                    node.lineno,
                )
            if bound.declared_type is not MacroType.object_:
                raise MacroRejection(
                    f"{what} is `{node.id}`, which is declared "
                    f"`{bound.declared_type.value}`. A target names an object, so the "
                    "name has to be declared `object`.",
                    node.lineno,
                )
            return MacroNameTarget(name=node.id)

        literal = _literal(node)
        if literal is not None:
            raise MacroRejection(
                f"{what} cannot be written out as a value. A macro has no syntax for a "
                "coordinate or a `#id`, on purpose: both go stale between the run that "
                "wrote the macro and the run that calls it. Aim with "
                "`selector(\"<path>\")`, a parameter, or a name bound by `find(...)`.",
                getattr(node, "lineno", None),
            )
        raise MacroRejection(
            f"{what} is not a target. It is `selector(\"<path>\")`, a parameter on the "
            "`def` line, or a name bound by `find(...)` or `selector(...)`.",
            getattr(node, "lineno", None),
        )

    # -- 호출 statement --

    def _call_statement(self, node: ast.Call) -> MacroStatement:
        if not isinstance(node.func, ast.Name):
            raise MacroRejection(
                "a statement calls a plain name. A dot is not allowed anywhere in a "
                "macro.",
                node.lineno,
            )
        name = node.func.id
        written = _written(node, self.lines)

        if name == REQUIRE:
            return self._require(node, written)
        if name == FLAG:
            return self._flag(node, written)
        if name == ASK_VERDICT:
            return self._ask_verdict(node, written)

        if name in TOOLS_BY_NAME:
            spec = TOOLS_BY_NAME[name]
            return MacroActionStatement(
                source=written,
                callee=name,
                arguments=self._arguments(spec.name, spec.parameters, node),
            )

        if name in self.signatures:
            helper = self.signatures[name]
            return MacroHelperCallStatement(
                source=written,
                callee=name,
                arguments=self._arguments(
                    helper.name, _helper_parameters(helper), node
                ),
            )

        self._reject_callee(name, node)

    def _reject_callee(self, name: str, node: ast.Call) -> None:
        if name in FORBIDDEN_TOOLS:
            raise MacroRejection(f"{FORBIDDEN_TOOLS[name]}.", node.lineno)
        if name in REPORTING_NAMES:
            raise MacroRejection(f"{REPORTING_NAMES[name]}.", node.lineno)
        if name in READERS_BY_NAME:
            raise MacroRejection(
                f"`{name}` reads a value, so it cannot stand on its own as a statement. "
                f"Put it in a condition: `require({name}(...) == ..., \"<remedy>\")`.",
                node.lineno,
            )
        if name in (FIND, SELECTOR):
            raise MacroRejection(
                f"`{name}(...)` names an object, so it cannot stand on its own as a "
                "statement. Bind it to a name (`card: object = "
                f"{name}(...)`) or write it in a target slot.",
                node.lineno,
            )
        helpers = ", ".join(sorted(self.signatures)) or "none"
        raise MacroRejection(
            f"`{name}` is not something a macro can call. The game tools are: "
            f"{TOOL_NAME_LIST}. The `def` names in this source are: {helpers}.",
            node.lineno,
        )

    def _require(self, node: ast.Call, written: str) -> MacroRequireStatement:
        if node.keywords:
            # `require(hp = 100)` 은 문법 오류가 아니다. Python 이 `hp = 100` 을
            # `ast.keyword` 로 읽어 조용히 통과시키므로, 거절 문장이 `==` 를 대 줘야 한다.
            first = node.keywords[0].arg or "**"
            raise MacroRejection(
                f"`{first} = ...` inside `require` is a keyword argument, not a "
                "comparison. To ask whether two things are equal, write `==`: "
                f"`require({first} == ..., \"<remedy>\")`.",
                node.lineno,
            )
        if len(node.args) != 2:
            raise MacroRejection(
                "`require` takes exactly two arguments: the condition, and a remedy "
                "written out as a string. The remedy is what the agent reading the "
                "failure is told to do about it — a failure that says only what is wrong "
                "leaves the next agent guessing.",
                node.lineno,
            )
        condition = self._condition(node.args[0])
        remedy = _string_literal(node.args[1], "the remedy of `require`").strip()
        if not remedy:
            raise MacroRejection(
                "the remedy of `require` cannot be empty. Write what to do when the "
                "condition is false, in one sentence.",
                node.lineno,
            )
        return MacroRequireStatement(source=written, condition=condition, remedy=remedy)

    def _flag(self, node: ast.Call, written: str) -> MacroFlagStatement:
        if node.keywords or len(node.args) != 1:
            raise MacroRejection(
                "`flag` takes exactly one argument: a message written out as a string. "
                "It only tells the agent what the macro saw — it names no step and sets "
                "no obligation. `ask_verdict(<step>, <expected>)` is the one that does.",
                node.lineno,
            )
        message = _string_literal(node.args[0], "the message of `flag`").strip()
        if not message:
            raise MacroRejection(
                "the message of `flag` cannot be empty. Say what the macro saw.",
                node.lineno,
            )
        return MacroFlagStatement(source=written, message=message)

    def _ask_verdict(self, node: ast.Call, written: str) -> MacroAskVerdictStatement:
        if node.keywords or len(node.args) != 2:
            raise MacroRejection(
                "`ask_verdict` takes exactly two arguments: the scenario step number, "
                "and what should be true at that step, written out as a string.",
                node.lineno,
            )
        step = _literal(node.args[0])
        if step is None or step.shape is not MacroShape.number or not isinstance(step.value, int):
            raise MacroRejection(
                "the first argument of `ask_verdict` is a scenario step number, written "
                "out as a whole number of 1 or more.",
                node.lineno,
            )
        if step.value < 1:
            raise MacroRejection(
                f"`ask_verdict` was given step {step.value}. Scenario steps start at 1.",
                node.lineno,
            )
        expected = _string_literal(
            node.args[1], "the expectation of `ask_verdict`"
        ).strip()
        if not expected:
            raise MacroRejection(
                "the expectation of `ask_verdict` cannot be empty. Write what should be "
                "true at that step — a sentence that is true whenever the macro runs, "
                "not what you happened to see on one run.",
                node.lineno,
            )
        return MacroAskVerdictStatement(source=written, step=step.value, expected=expected)

    # -- 인자 --

    def _arguments(
        self,
        callee: str,
        parameters: tuple[ToolParameter, ...],
        node: ast.Call,
    ) -> tuple[MacroArgument, ...]:
        """적힌 인자를 parameter 에 맞춰 bind 한다. 기본값은 여기서 채우지 않는다.

        채우지 않는 이유는 저장되는 JSON 이 저자가 적은 것만 들어야 하기 때문이다.
        tool 의 기본값은 `grammar.py` 의 표가 들고 있고, runner 가 부를 때 거기서 읽는다.
        """
        by_name = {parameter.name: parameter for parameter in parameters}
        if len(node.args) > len(parameters):
            raise MacroRejection(
                f"`{callee}` takes {_parameter_list(parameters)}, but was given "
                f"{len(node.args)} positional arguments.",
                node.lineno,
            )

        filled: dict[str, ast.expr] = {}
        for parameter, value in zip(parameters, node.args):
            filled[parameter.name] = value
        for keyword in node.keywords:
            if keyword.arg is None:
                raise MacroRejection(
                    f"`**` is not allowed in a call to `{callee}`.", node.lineno
                )
            if keyword.arg not in by_name:
                raise MacroRejection(
                    f"`{callee}` has no `{keyword.arg}=` argument. It takes "
                    f"{_parameter_list(parameters)}.",
                    node.lineno,
                )
            if keyword.arg in filled:
                raise MacroRejection(
                    f"`{keyword.arg}` is given twice in this call to `{callee}`.",
                    node.lineno,
                )
            filled[keyword.arg] = keyword.value

        missing = [
            parameter.name
            for parameter in parameters
            if parameter.required and parameter.name not in filled
        ]
        if missing:
            raise MacroRejection(
                f"`{callee}` is missing {', '.join(missing)}. It takes "
                f"{_parameter_list(parameters)}.",
                node.lineno,
            )

        arguments: list[MacroArgument] = []
        for parameter in parameters:
            value = filled.get(parameter.name)
            if value is None:
                continue
            arguments.append(self._argument(callee, parameter, value))
        return tuple(arguments)

    def _argument(
        self, callee: str, parameter: ToolParameter, node: ast.expr
    ) -> MacroArgument:
        what = f"`{parameter.name}` of `{callee}`"
        if parameter.is_target:
            return MacroTargetArgument(
                parameter=parameter.name, target=self._target(node, what)
            )

        literal = _literal(node)
        if literal is not None:
            _reject_type_mismatch(parameter.declared_type, literal, what, node)
            return MacroLiteralArgument(parameter=parameter.name, literal=literal)

        if isinstance(node, ast.Name):
            bound = self._require_bound(node)
            _reject_name_mismatch(parameter.declared_type, bound.declared_type, what, node)
            return MacroNameArgument(
                parameter=parameter.name,
                name=node.id,
                declared_type=bound.declared_type,
            )

        if isinstance(node, ast.Attribute):
            raise MacroRejection(f"{_NO_DOT}.", node.lineno)
        raise MacroRejection(
            f"{what} is neither a literal nor a bare name. An argument that is not a "
            "target is one literal (an int, a float, a string or a bool) or a name bound "
            "earlier in this body.",
            getattr(node, "lineno", None),
        )


def _reject_name_mismatch(
    wanted: MacroType, given: MacroType, what: str, node: ast.AST
) -> None:
    if wanted is given:
        return
    # 선언이 `int` 인 이름은 `float` 자리에 들어간다. 반대는 아니다 — 리터럴의 규칙과
    # 같고, 같은 이유다.
    if wanted is MacroType.float_ and given is MacroType.int_:
        return
    raise MacroRejection(
        f"{what} wants `{wanted.value}` but was given a name declared `{given.value}`.",
        getattr(node, "lineno", None),
    )


def _helper_parameters(signature: _Signature) -> tuple[ToolParameter, ...]:
    """helper `def` 의 parameter 를 tool 인자 검사와 같은 모양으로 옮긴다.

    helper 를 부르는 자리는 tool 호출 statement 와 같은 자리이므로 검사도 같아야 한다.
    `object` 로 선언된 parameter 가 target 자리가 된다 — 그 helper 가 그 이름을 조준에
    쓸 수 있는 유일한 타입이다.
    """
    return tuple(
        ToolParameter(
            name=parameter.name,
            declared_type=parameter.declared_type,
            is_target=parameter.declared_type is MacroType.object_,
            default=None if parameter.default is None else parameter.default.value,
            required=parameter.default is None,
        )
        for parameter in signature.parameters
    )


def _parameter_list(parameters: tuple[ToolParameter, ...]) -> str:
    if not parameters:
        return "no arguments"
    return ", ".join(
        f"{parameter.name}: {parameter.declared_type.value}"
        + ("" if parameter.required else f" = {parameter.default!r}")
        for parameter in parameters
    )


def _reader_arguments(spec: ReaderSpec) -> str:
    parts: list[str] = []
    if spec.takes_target:
        parts.append("one target")
    if spec.takes_key:
        parts.append("one plain string")
    return " and ".join(parts) if parts else "no arguments"


# --- 저장 시점에 정적으로 세는 것 ------------------------------------------------
#
# 네 가지를 센다. 재귀, 호출 깊이, 가장 많이 도는 경로의 statement 수, 그리고 눌렀으면
# 풀어야 하는 tool 의 카운터. 넷 다 같은 호출 그래프를 걷는다.
#
# **걷는 자리를 하나로 둔다.** 네 검사가 각자 그래프를 다시 세우고 각자 memo 를 손으로
# 달면, memo 를 빠뜨린 검사 하나가 지수로 터진다 — 실제로 `depth` 하나가 그랬다. helper
# 하나를 두 번 부르는 macro 는 층마다 일이 두 배가 되어 helper 스물넷(99줄)에서 5초,
# 서른에서 5분이 걸렸고, 그것이 `write_macro` tool 호출 안이라 서버 coroutine 을 그만큼
#막는다. 모델이 helper 를 단계마다 하나씩 쓰는 macro 를 내놓는 것은 공격이 아니라
# 평범한 경우다.


def _called_helpers(statements: tuple[MacroStatement, ...]) -> list[str]:
    """그 몸통이 부르는 helper 이름 전부, 부른 순서대로. 분기 안쪽까지 센다.

    같은 helper 를 두 번 부르면 두 번 적힌다. 세는 쪽이 그것을 알아야 statement 총수가
    맞는다.
    """
    names: list[str] = []
    for statement in statements:
        if isinstance(statement, MacroHelperCallStatement):
            names.append(statement.callee)
        elif isinstance(statement, MacroIfStatement):
            names.extend(_called_helpers(statement.body))
            names.extend(_called_helpers(statement.orelse))
    return names


class _CallGraph:
    """`def` 들이 서로를 부르는 모양. 네 검사가 여기에 묻는다.

    답을 전부 memo 한다. 그래프는 저장 시점에 고정이고 질문은 되풀이되므로, 같은 답을
    두 번 세는 것은 그대로 지수가 된다.
    """

    def __init__(self, definition: MacroDefinition) -> None:
        self.definition = definition
        self.calls: dict[str, list[str]] = {
            definition.entry.name: _called_helpers(definition.entry.statements)
        }
        for helper in definition.helpers:
            self.calls[helper.name] = _called_helpers(helper.statements)
        self._depth: dict[str, int] = {}
        self._statements: dict[str, int] = {}
        self._tally: dict[tuple[str, str], _Tally] = {}

    def statements_of(self, name: str) -> tuple[MacroStatement, ...]:
        function = self.definition.function(name)
        return () if function is None else function.statements

    # -- 재귀 --

    def reject_recursion(self) -> None:
        """자기 호출도 상호 재귀도 거절한다.

        제일 먼저 묻는다. 아래 셋이 호출 트리를 걷기 때문이다 — 고리가 남아 있으면
        세는 쪽이 영원히 돈다.
        """
        walking: list[str] = []
        settled: set[str] = set()

        def walk(name: str) -> None:
            if name in walking:
                cycle = " → ".join(walking[walking.index(name) :] + [name])
                raise MacroRejection(
                    f"this macro calls itself: {cycle}. A macro has no recursion — how "
                    "many statements it would turn through could not be counted when it "
                    "is stored, and that count is what the 128-statement limit is "
                    "checked against."
                )
            if name in settled:
                return
            walking.append(name)
            for called in self.calls.get(name, ()):
                walk(called)
            walking.pop()
            settled.add(name)

        for name in self.calls:
            walk(name)

    # -- 호출 깊이 --

    def depth_of(self, name: str) -> int:
        if name not in self._depth:
            called = self.calls.get(name, ())
            # 먼저 넣어 두지 않는다. 고리는 `reject_recursion` 이 이미 걸렀으므로 여기
            # 닿는 그래프는 비순환이다.
            self._depth[name] = 1 + max(
                (self.depth_of(one) for one in called), default=0
            )
        return self._depth[name]

    def reject_over_depth(self) -> None:
        measured = self.depth_of(self.definition.entry.name)
        if measured > MAX_CALL_DEPTH:
            raise MacroRejection(
                f"the call chain in this macro is {measured} deep, and the limit is "
                f"{MAX_CALL_DEPTH} counting the entry point. A failure payload prints "
                "the chain it happened in, and four links do not read in one payload. "
                "Flatten a helper into its caller."
            )

    # -- statement 총수 --

    def statements_in(self, name: str) -> int:
        if name not in self._statements:
            self._statements[name] = self._count(self.statements_of(name))
        return self._statements[name]

    def _count(self, statements: tuple[MacroStatement, ...]) -> int:
        """그 몸통에서 가장 많이 도는 경로의 statement 수.

        `if` 는 적힌 statement 를 세는 것을 바꾸지 않는다 — 최대값은 가지들 중 큰 쪽이라
        여전히 정적이다. helper 호출은 호출 그 자체 하나에 그 helper 의 총수를 더한다.

        세는 것만 편다, 실행은 안 편다. 상한을 세려고 호출 트리를 펴는 것이지 실행할 때
        펴는 것이 아니다.
        """
        total = 0
        for statement in statements:
            if isinstance(statement, MacroIfStatement):
                total += 1 + max(
                    self._count(statement.body), self._count(statement.orelse)
                )
            elif isinstance(statement, MacroHelperCallStatement):
                total += 1 + self.statements_in(statement.callee)
            else:
                total += 1
        return total

    def reject_over_statements(self) -> None:
        measured = self.statements_in(self.definition.entry.name)
        if measured > MAX_STATEMENTS:
            raise MacroRejection(
                f"the longest path through this macro turns {measured} statements, and "
                f"the limit is {MAX_STATEMENTS}. Caught here rather than part-way "
                "through a run: once an action has gone to the game it cannot be taken "
                "back. Split the macro, or drop statements from the heaviest branch."
            )

    # -- 눌렀으면 풀어야 하는 tool --

    def tally_of(self, name: str, pair: PairedTools) -> "_Tally":
        if (name, pair.counter) not in self._tally:
            self._tally[(name, pair.counter)] = self._tally_body(
                self.statements_of(name), pair
            )
        return self._tally[(name, pair.counter)]

    def _tally_body(
        self, statements: tuple[MacroStatement, ...], pair: PairedTools
    ) -> "_Tally":
        """그 몸통의 카운터 합과, 지나는 동안 내려간 최저값.

        `if` 의 두 가지가 서로 다른 `delta` 를 내면 거기서 거절한다. 경로를 하나하나
        펼치지 않는 이유는 `if` 하나당 경로가 둘로 갈려 128 statement 면 경로가
        천문학적인 수가 되기 때문이고, 두 가지가 같은 값을 남기면 모든 경로의 합이
        하나로 정해지므로 합 하나로 판정할 수 있다.

        그리고 **한 분기에서만 누르고 다른 분기에서 푸는 macro** 가 정확히 두 가지가
        다른 값을 내는 경우다. 그것이 맞는 거절이다.
        """
        delta = 0
        lowest = 0
        for statement in statements:
            if isinstance(statement, MacroActionStatement):
                step = (
                    1
                    if statement.callee == pair.opens
                    else -1
                    if statement.callee == pair.closes
                    else 0
                )
                delta += step
                lowest = min(lowest, delta)
                continue
            if isinstance(statement, MacroHelperCallStatement):
                inner = self.tally_of(statement.callee, pair)
            elif isinstance(statement, MacroIfStatement):
                taken = self._tally_body(statement.body, pair)
                other = self._tally_body(statement.orelse, pair)
                if taken.delta != other.delta:
                    raise MacroRejection(
                        f"{pair.counter} is left in a different state by the two "
                        f"branches of `{statement.source}`: one leaves "
                        f"{taken.delta:+d} and the other {other.delta:+d}. Hold and "
                        "release it inside the same branch, or in neither — a macro "
                        "that presses in one branch and lets go in the other leaves "
                        "the game holding it whenever the condition goes the other way."
                    )
                inner = _Tally(
                    delta=taken.delta, lowest=min(taken.lowest, other.lowest)
                )
            else:
                continue
            lowest = min(lowest, delta + inner.lowest)
            delta += inner.delta
        return _Tally(delta=delta, lowest=lowest)

    def reject_unpaired(self) -> None:
        """누른 것을 안 푼 macro 를 거절한다.

        tool 을 직접 부르는 agent 는 그 tool 의 docstring("Nothing releases this for
        you.")을 다음 턴에 다시 읽는다. macro 는 글이 저작 시점에 고정이라 읽어 줄 다음
        턴이 없고, 눌린 채로 남은 키는 그 뒤의 모든 step 을 조용히 바꾼다. 반복이 없어
        경로가 유한하므로 여기서 셀 수 있다.
        """
        for pair in PAIRED_TOOLS:
            total = self.tally_of(self.definition.entry.name, pair)
            if total.delta > 0:
                raise MacroRejection(
                    f"this macro leaves {pair.counter} behind: it calls "
                    f"`{pair.opens}` {total.delta} more time(s) than "
                    f"`{pair.closes}`. Nothing releases it for you, so every step after "
                    f"this macro would run with it still held. Add the matching "
                    f"`{pair.closes}`."
                )
            if total.delta < 0 or total.lowest < 0:
                raise MacroRejection(
                    f"this macro releases {pair.counter} it never took: "
                    f"`{pair.closes}` is reached without a `{pair.opens}` before it. "
                    f"Either the `{pair.opens}` is missing or the two are the wrong way "
                    "round."
                )


@dataclass(frozen=True)
class _Tally:
    """한 몸통이 카운터 하나에 하는 일.

    `delta` 는 그 몸통을 지나면 카운터가 얼마나 달라지나, `lowest` 는 그 몸통을 지나는
    동안 카운터가 내려간 최저값이다. 둘을 함께 들면 분기가 있어도 경로를 하나하나
    펼치지 않고 셀 수 있다.
    """

    delta: int
    lowest: int
