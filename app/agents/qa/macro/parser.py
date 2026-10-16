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
import inspect
from collections.abc import Mapping
from dataclasses import dataclass

from app.agents.qa.macro.errors import MacroRejection
from app.agents.qa.macro.grammar import (
    ARITHMETIC_FUNCTIONS,
    ASK,
    ASK_VERDICT,
    CHECKPOINT,
    DECIDE,
    DECLARABLE_TYPE_NAMES,
    DECLARABLE_TYPES,
    FIND,
    FIND_ALL,
    FIND_KEYWORDS,
    FIND_REQUIRED_KEYWORDS,
    FLAG,
    FORBIDDEN_TOOLS,
    IMPORT_MODULE,
    MAX_CALL_DEPTH,
    MAX_LOOP_PASSES,
    MAX_STATEMENTS,
    MAX_WAIT_PER_STATEMENT_SECONDS,
    MIN_MATCH_CASES,
    NONE_FITS,
    NUMBER_RETURNS,
    PAIRED_TOOLS,
    PairedTools,
    READER_NAME_LIST,
    READERS_BY_NAME,
    ReaderSpec,
    REPORTING_NAMES,
    RANGE,
    REQUIRE,
    RESERVED_CONTEXT_KEYS,
    WAIT_UNTIL,
    SELECTOR,
    TOOL_NAME_LIST,
    TOOLS_BY_NAME,
    UNDER,
    MacroArithmetic,
    MacroOperator,
    MacroShape,
    MacroType,
    ToolParameter,
    ToolSpec,
    allowed_operators,
    arithmetic_allowed,
    comparison_allowed,
    operator_names,
)
from app.agents.qa.macro.model import (
    MacroActionStatement,
    MacroArgument,
    MacroArithmeticOperand,
    MacroAskCondition,
    MacroAskVerdictStatement,
    MacroAssignStatement,
    MacroAssignValue,
    MacroCheckpointStatement,
    MacroComparisonCondition,
    MacroComputeValue,
    MacroCondition,
    MacroContextArgument,
    MacroContextFindAll,
    MacroContextLiteral,
    MacroContextName,
    MacroContextTarget,
    MacroContextUnder,
    MacroDecideCall,
    MacroDefinition,
    MacroExtremumOperand,
    MacroFindAllIterable,
    MacroFindValue,
    MacroFlagStatement,
    MacroForStatement,
    MacroFunction,
    MacroHelperCallStatement,
    MacroIfStatement,
    MacroIterable,
    MacroLiteral,
    MacroLiteralArgument,
    MacroLiteralOperand,
    MacroLiteralValue,
    MacroMatchCase,
    MacroMatchStatement,
    MacroRangeIterable,
    MacroReadValue,
    MacroNameArgument,
    MacroNameOperand,
    MacroNameTarget,
    MacroNumberCallOperand,
    MacroOperand,
    MacroParameter,
    MacroPredicateCondition,
    MacroReaderCall,
    MacroReaderCondition,
    MacroReaderOperand,
    MacroRequireStatement,
    MacroReturnStatement,
    MacroSelectorOperand,
    MacroSelectorTarget,
    MacroSelectorValue,
    MacroStatement,
    MacroStringLiteral,
    MacroStringName,
    MacroStringRef,
    MacroTarget,
    MacroTargetArgument,
    MacroWhileStatement,
    MacroWaitUntilStatement,
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

# 숫자 `def` 안의 이항 연산. 단항 `-` 는 `0 - x` 로 읽어 `subtract` 가 받는다.
_ARITHMETIC_OPERATORS: dict[type[ast.operator], MacroArithmetic] = {
    ast.Add: MacroArithmetic.add,
    ast.Sub: MacroArithmetic.subtract,
    ast.Mult: MacroArithmetic.multiply,
    ast.Div: MacroArithmetic.divide,
}
_ARITHMETIC_NAMES = "`+`, `-`, `*`, `/`, `min(...)` and `max(...)`"

# 숫자 `def` 의 `return` 과 대입의 오른쪽, 산술의 피연산자가 받는 것. 거절 문장 여럿이
# 같은 말을 한다.
_NUMBER_FORMS = (
    "one number literal, a name declared `int` or `float`, a reader call "
    f"({READER_NAME_LIST}, at_start()), a call to another `-> int` or `-> float` def, or "
    f"arithmetic over those with {_ARITHMETIC_NAMES}"
)

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
    "an action call (one of the game tools, a helper `def` in this same source, or a "
    "macro imported with `from macros import ...`), "
    "`require(<condition>, <remedy>)`, `wait_until(<condition>, <seconds>, <remedy>)`, "
    "a typed assignment (`name: object = find(...)`), "
    "`if`, `for`, `while`, `match decide(...)` over imported macros, `flag(<message>)`, "
    "`ask_verdict(<expected>)`, or `checkpoint(<reason>)`"
)

# `decide`·`ask` 의 keyword 인자 하나가 받는 다섯 모양. 거절 문장 세 곳이 같은 말을 한다.
_CONTEXT_FORMS = (
    "a name declared `object` (a parameter, a `find(...)` or `selector(...)` name, or a "
    "`for` name), `selector(\"<path>\")`, `find_all(label=..., name=..., under=...)`, "
    "`under(\"<path>\")` or `under(<object name>)` for an object and everything below it, a "
    "name bound earlier to an `int`, `float`, `string` or `bool`, or one literal (an int, "
    "a float, a string or a bool)"
)

# 몸통 안의 `decide(...)` 를 `match` 밖에서 만났을 때의 거절 문장.
_DECIDE_ONLY_IN_MATCH = (
    "`decide(...)` stands only as the subject of a `match`: write "
    '`match decide("<question>", key=value, ...):` with one `case "<imported macro>":` '
    "per option. Jev picks the case; it hands back no value to bind or compare."
)

# `ask(...)` 가 조건 전체가 아닌 자리에 왔을 때의 거절 문장.
_ASK_ONLY_AS_CONDITION = (
    '`ask("<question>", key=value, ...)` stands only as the whole condition of `if`, '
    "`elif`, `while` or `require` in a `-> None` def. It cannot be compared, combined, "
    "bound to a name, or written on its own line."
)


# reader 를 bind 한 이름의 `origin`. target 자리 거절이 읽는다.
_READ = "read"
# `for` 가 bind 한 이름의 `origin`.
_LOOP = "loop"
# 숫자 `def` 안에서 산술식이나 숫자 `def` 호출을 bind 한 이름의 `origin`.
_COMPUTE = "compute"

# 도착 모양이 정해진 reader 를 bind 할 때 선언할 수 있는 타입. `number` 는 `int` 와
# `float` 둘 다 받는다 — SDK 가 반올림해 보내므로 도착한 값만 보고는 둘을 못 가른다
# (`grammar.MacroShape`).
_DECLARED_FOR_SHAPE: dict[MacroShape, tuple[MacroType, ...]] = {
    MacroShape.number: (MacroType.int_, MacroType.float_),
    MacroShape.string: (MacroType.string,),
    MacroShape.bool_: (MacroType.bool_,),
}


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
    returns: str


def _value_def_noun(returns: str) -> str:
    """값을 돌려주는 `def` 를 거절 문장이 부르는 말.

    predicate 는 숫자 `def` 가 생기기 전의 문장을 글자 그대로 지키려고 "predicate" 로 둔다.
    """
    return "predicate" if returns == "bool" else "def"


def _ends_in_return(statements: tuple[MacroStatement, ...]) -> bool:
    if not statements:
        return False
    last = statements[-1]
    if isinstance(last, MacroReturnStatement):
        return True
    if isinstance(last, MacroIfStatement):
        return _ends_in_return(last.body) and _ends_in_return(last.orelse)
    return False


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


def imported_names(source: str) -> tuple[str, ...]:
    """`from macros import a, b` 로 가져오는 이름, 적힌 순서대로. 겹친 이름은 한 번만.

    tool 계층이 파싱 **전에** 가져올 macro 를 찾는 데 쓴다. 등록된 macro 를 읽는 것은 async
    이고 `macro_definition_from_source` 는 순수 함수라서, 가져온 정의를 먼저 모아 넘긴다.
    검사는 하지 않는다 — 문법 오류면 `()` 를 내고, 그 거절은 본 파싱이 줄 번호와 함께 한다.
    """
    try:
        module = ast.parse(source)
    except SyntaxError:
        return ()
    names: list[str] = []
    for node in module.body:
        if not (
            isinstance(node, ast.ImportFrom)
            and node.module == IMPORT_MODULE
            and node.level == 0
        ):
            continue
        for alias in node.names:
            if alias.name != "*" and alias.name not in names:
                names.append(alias.name)
    return tuple(names)


def macro_definition_from_source(
    name: str,
    source: str,
    imports: Mapping[str, MacroDefinition] | None = None,
) -> MacroDefinition:
    """macro 텍스트 하나를 통과시켜 정의로 만든다. 못 통과하면 `MacroRejection`.

    정의를 만드는 **유일한** 길이다. JSON 만 고치는 경로를 만들지 않는 것이 이 모듈과
    `model.py` 의 계약이고, 그래서 `MacroDefinition` 은 여기서만 생긴다.

    `imports` 는 `from macros import ...` 로 가져오는 이름 → 이미 파싱된 등록 macro 다.
    가져오는 이름은 전부 key 여야 하고, 남는 key 는 무시한다.
    """
    module = _module(source)
    lines = source.splitlines()
    import_nodes, functions = _top_level_statements(module)
    signatures = _signatures(name, functions)
    if signatures[name].returns == "bool":
        raise MacroRejection(
            "the entry point acts; a `-> bool` def is a predicate that a condition "
            "calls. Declare the entry point `-> None`.",
            next(node.lineno for node in functions if node.name == name),
        )
    if signatures[name].returns in NUMBER_RETURNS:
        raise MacroRejection(
            f"the entry point acts; a `-> {signatures[name].returns}` def computes a "
            "number that a condition compares. Declare the entry point `-> None`.",
            next(node.lineno for node in functions if node.name == name),
        )
    imported = _imported_definitions(name, import_nodes, signatures, imports or {})
    imported_signatures = {
        definition.name: _Signature(
            definition.name, definition.entry.parameters, "none"
        )
        for definition in imported
    }

    entry: MacroFunction | None = None
    helpers: list[MacroFunction] = []
    for node in functions:
        reader = _FunctionReader(signatures, node.name, lines, imported_signatures)
        docstring, body_nodes = _split_docstring(node)
        built = MacroFunction(
            name=node.name,
            parameters=signatures[node.name].parameters,
            docstring=docstring,
            returns=signatures[node.name].returns,
            statements=reader.body(body_nodes, node),
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
        name=name,
        source=source,
        entry=entry,
        helpers=tuple(helpers),
        imports=tuple(imported),
    )
    # 네 검사가 같은 호출 그래프에 묻는다. 그래프를 네 번 다시 세우고 memo 를 네 번
    # 손으로 달면, 빠뜨린 하나가 지수로 터진다.
    graph = _CallGraph(definition)
    graph.reject_recursion()
    graph.reject_over_depth()
    graph.reject_over_statements()
    graph.reject_unpaired()
    # 짝이 맞는다는 판정 뒤에 둔다. 그래야 각 자리의 카운터 값이 하나로 정해진다.
    graph.reject_checkpoint_while_holding()
    return definition


# --- module 과 `def` -----------------------------------------------------------


def _module(source: str) -> ast.Module:
    if not source.strip():
        raise MacroRejection(
            "A macro source cannot be empty. Write one `def` named after the macro, "
            f"whose parameters and return are typed: {DECLARABLE_TYPE_NAMES} for a "
            "parameter, `-> None` for the entry point, and `-> bool` for a predicate."
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


def _split_docstring(node: ast.FunctionDef) -> tuple[str | None, list[ast.stmt]]:
    """`def` 몸통의 첫 statement 가 문자열 하나면 그것을 docstring 으로 떼어 낸다.

    docstring 은 statement 가 아니다. `statements` 에 들어가지 않으므로 statement 한도에
    세지 않고 runner 도 보지 않는다. entry `def` 의 docstring 이 이 macro 의 summary 로
    다음 런의 첫 메시지와 `search_macros` 결과에 실린다.

    공백뿐인 docstring 은 `None` 이다. summary 자리에 빈 줄을 그리면 summary 가 없는
    macro 와 구별되지 않는다. 몸통이 docstring 하나뿐이면 남은 몸통이 비어서
    `_FunctionReader.body` 가 빈 몸통으로 거절한다.
    """
    first = node.body[0] if node.body else None
    if not (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return None, list(node.body)
    cleaned = inspect.cleandoc(first.value.value).strip()
    return (cleaned or None), list(node.body[1:])


def _top_level_statements(
    module: ast.Module,
) -> tuple[list[ast.ImportFrom], list[ast.FunctionDef]]:
    """맨 위의 `from macros import ...` 들과 `def` 들. 그 밖은 거절한다.

    import 는 첫 `def` 보다 앞에만 선다. 읽는 사람이 macro 가 무엇을 부를 수 있는지를 맨
    위에서 한 번에 보게 하려는 것이다.
    """
    import_nodes: list[ast.ImportFrom] = []
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
            _reject_import_form(node, after_def=bool(functions))
            import_nodes.append(node)  # type: ignore[arg-type]
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            raise MacroRejection(
                "a bare string is not allowed at the top of a macro source. A docstring "
                "goes as the first statement of a `def`, indented under its `def` line; "
                "anywhere else, use a `#` comment.",
                node.lineno,
            )
        raise MacroRejection(
            "only `def` and `from macros import ...` are allowed at the top of a macro "
            "source. The `def` named after the macro is its entry point and every other "
            "`def` is a helper it may call.",
            node.lineno,
        )
    if not functions:
        raise MacroRejection(
            "A macro source needs at least one `def`: the one named after the macro."
        )
    return import_nodes, functions


# import 거절 문장이 모두 끝에 다는 말. 받는 모양 하나를 그대로 적어 준다.
_IMPORT_FORM = (
    f"The one import a macro takes is `from {IMPORT_MODULE} import a, b`, at the top of "
    "the source before the first `def`, naming registered macros to call like helpers."
)


def _reject_import_form(node: ast.Import | ast.ImportFrom, after_def: bool) -> None:
    """`from macros import a, b` 가 아닌 import 를 거절한다. 이름 검사는 따로 한다."""
    line = node.lineno
    if isinstance(node, ast.Import):
        raise MacroRejection(
            "a plain `import x` is not allowed: a macro calls nothing outside the game "
            f"tools ({TOOL_NAME_LIST}), the readers ({READER_NAME_LIST}), and registered "
            f"macros. {_IMPORT_FORM}",
            line,
        )
    if after_def:
        raise MacroRejection(
            f"an import after a `def` is not allowed. {_IMPORT_FORM}", line
        )
    if node.level != 0:
        raise MacroRejection(
            f"a relative import (`from .{node.module or ''} import ...`) is not "
            f"allowed. {_IMPORT_FORM}",
            line,
        )
    if node.module != IMPORT_MODULE:
        raise MacroRejection(
            f"`from {node.module} import ...` is not allowed: registered macros are "
            f"the only thing a macro imports. {_IMPORT_FORM}",
            line,
        )
    for alias in node.names:
        if alias.name == "*":
            raise MacroRejection(
                f"`from {IMPORT_MODULE} import *` is not allowed; name each macro you "
                f"call. {_IMPORT_FORM}",
                line,
            )
        if alias.asname is not None:
            raise MacroRejection(
                f"`import {alias.name} as {alias.asname}` is not allowed: an imported "
                "macro keeps its registered name, so the call reads the same as the "
                f"macro it runs. {_IMPORT_FORM}",
                line,
            )


def _imported_definitions(
    name: str,
    import_nodes: list[ast.ImportFrom],
    signatures: dict[str, _Signature],
    imports: Mapping[str, MacroDefinition],
) -> list[MacroDefinition]:
    """가져오는 이름을 import 순서대로 정의에 맞춘다.

    `def` 와 같은 이름은 거절한다 — 한 이름이 둘을 가리키면 어느 쪽이 도는지를 읽는 사람이
    알 수 없다. 자기 자신을 가져오는 것은 재귀이므로 거절한다.
    """
    definitions: list[MacroDefinition] = []
    seen: set[str] = set()
    for node in import_nodes:
        for alias in node.names:
            imported = alias.name
            if imported in seen:
                raise MacroRejection(
                    f"`{imported}` is imported twice. Name each macro once.",
                    node.lineno,
                )
            seen.add(imported)
            if imported == name:
                raise MacroRejection(
                    f"this macro imports itself (`{imported}`). A macro has no "
                    "recursion; import only other registered macros.",
                    node.lineno,
                )
            if imported in signatures:
                raise MacroRejection(
                    f"`{imported}` is both imported and written as a `def` in this "
                    "source, so a call to it could mean either. Rename the `def`, or "
                    "remove it from the import.",
                    node.lineno,
                )
            definition = imports.get(imported)
            if definition is None:
                raise MacroRejection(
                    f"`{imported}` is imported but no registered macro has that name. "
                    "Register it first, or remove it from the import.",
                    node.lineno,
                )
            definitions.append(definition)
    return definitions


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
        parameters, returns = _parameters(node)
        signatures[node.name] = _Signature(node.name, parameters, returns)

    if name not in signatures:
        written = ", ".join(sorted(signatures))
        raise MacroRejection(
            f"this macro is called {name!r}, so it needs a top-level `def {name}(...)` "
            f"to be its entry point. The source defines: {written}."
        )
    return signatures


def _parameters(node: ast.FunctionDef) -> tuple[tuple[MacroParameter, ...], str]:
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
            f"`def {node.name}` has no return type. Write `-> None` for an action "
            "def or `-> bool` for a predicate called in a condition.",
            node.lineno,
        )
    if isinstance(node.returns, ast.Constant) and node.returns.value is None:
        returns = "none"
    elif isinstance(node.returns, ast.Name) and node.returns.id == "bool":
        returns = "bool"
    elif isinstance(node.returns, ast.Name) and node.returns.id in NUMBER_RETURNS:
        # 숫자 `def` (ARTEL-1066). 위아래 거절 문장은 그것이 생기기 전 그대로 둔다 — QA
        # agent 의 skill 이 아직 숫자 `def` 를 가르치지 않는다.
        returns = node.returns.id
    else:
        raise MacroRejection(
            f"`def {node.name}` must be declared `-> None` for an action def or "
            "`-> bool` for a predicate called in a condition.",
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
    return tuple(parameters), returns


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
        self,
        signatures: dict[str, _Signature],
        name: str,
        lines: list[str],
        imported: dict[str, _Signature] | None = None,
    ) -> None:
        self.signatures = signatures
        # `from macros import ...` 로 가져온 macro 의 entry. helper 처럼 statement 로만
        # 부른다. predicate 는 가져올 수 없다 — entry 는 언제나 `-> None` 이다.
        self.imported = imported or {}
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
            "either a parameter on the `def` line, or a name bound earlier in this same "
            "body by an assignment or by the `for` it is inside. A name bound inside an "
            f"`if` or a loop ends with that body. Bound here: {bound}.",
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
        self,
        nodes: list[ast.stmt],
        owner: ast.AST,
        loop_name: tuple[str, MacroType] | None = None,
    ) -> tuple[MacroStatement, ...]:
        if not nodes:
            raise MacroRejection(
                "an empty body is not allowed. A body holds at least one statement: "
                f"{_STATEMENT_KINDS}.",
                getattr(owner, "lineno", None),
            )
        self.scopes.append({})
        try:
            if loop_name is not None:
                # `for` 의 이름은 그 몸통 안에서만 산다. 대입과 같은 규칙으로 bind 하므로
                # parameter 나 바깥 이름을 가리는 것도 같은 거절을 받는다.
                self._bind(loop_name[0], loop_name[1], _LOOP, owner)
            returns = self._returns()
            statements: list[MacroStatement] = []
            for node in nodes:
                if self._returns_value() and _ends_in_return(tuple(statements)):
                    raise MacroRejection(
                        "a statement after `return` cannot run. End this "
                        f"{_value_def_noun(returns)} body at the `return`.",
                        node.lineno,
                    )
                statements.append(self._statement(node))
            built = tuple(statements)
            if isinstance(owner, ast.FunctionDef) and self._returns_value():
                if not _ends_in_return(built):
                    raise MacroRejection(
                        f"every path through a `-> {returns}` "
                        f"{_value_def_noun(returns)} must end in `return`.",
                        owner.lineno,
                    )
            return built
        finally:
            # 몸통이 끝나면 그 안에서 bind 한 이름도 끝난다. 밖에서 쓰면 bind 되지 않은
            # 이름이 되므로, 버리는 것 자체가 그 거절이다.
            self.scopes.pop()

    def _statement(self, node: ast.stmt) -> MacroStatement:
        # 숫자 `def` 도 predicate 와 같은 규칙을 받는다. 둘 다 조건이나 식 안에서 동기로
        # 평가되므로 action·대기·Jev·`checkpoint`·`flag` 를 품을 수 없다.
        if self._returns_value():
            returns = self._returns()
            if isinstance(node, ast.Return):
                return self._return(node)
            if not isinstance(node, (ast.AnnAssign, ast.If)):
                described = (
                    "a predicate" if returns == "bool" else f"a `-> {returns}` def"
                )
                jev = (
                    f" `match decide(...)` calls Jev, and {described} is evaluated "
                    "synchronously."
                    if isinstance(node, ast.Match)
                    else ""
                )
                raise MacroRejection(
                    f"a `-> {returns}` {_value_def_noun(returns)} allows only a typed "
                    f"assignment, `if`, and `return`.{jev}",
                    node.lineno,
                )
        if isinstance(node, ast.AnnAssign):
            return self._assignment(node)
        if isinstance(node, ast.If):
            return self._conditional(node)
        if isinstance(node, ast.For):
            return self._for(node)
        if isinstance(node, ast.While):
            return self._while(node)
        if isinstance(node, ast.Match):
            return self._match(node)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return self._call_statement(node.value)
        self._reject_statement(node)

    def _returns(self) -> str:
        return self.signatures[self.name].returns

    def _is_predicate(self) -> bool:
        return self._returns() == "bool"

    def _returns_number(self) -> bool:
        return self._returns() in NUMBER_RETURNS

    def _returns_value(self) -> bool:
        """predicate 이거나 숫자 `def` 다. 둘이 같은 몸통 규칙을 받는다."""
        return self._returns() != "none"

    def _return(self, node: ast.Return) -> MacroReturnStatement:
        if self._returns_number():
            return self._number_return(node)
        if node.value is None:
            raise MacroRejection(
                "a `-> bool` predicate must return `True`, `False`, or a condition; "
                "bare `return` is not allowed.",
                node.lineno,
            )
        source = _written(node, self.lines)
        if isinstance(node.value, ast.Constant) and type(node.value.value) is bool:
            return MacroReturnStatement(source=source, value=node.value.value)
        return MacroReturnStatement(source=source, condition=self._condition(node.value))

    # -- 숫자 `def` 와 산술식 (ARTEL-1066) --

    def _number_return(self, node: ast.Return) -> MacroReturnStatement:
        returns = self._returns()
        if node.value is None:
            raise MacroRejection(
                f"a `-> {returns}` def must return a number; bare `return` is not "
                f"allowed. It returns {_NUMBER_FORMS}.",
                node.lineno,
            )
        expression = self._number_value(node.value, f"what `{self.name}` returns")
        if returns == MacroType.int_.value and self._may_have_fraction(expression):
            raise MacroRejection(
                f"`{self.name}` is declared `-> int`, but what it returns can have a "
                "fraction — it divides with `/`, or uses a float literal, a name declared "
                "`float` or a `-> float` def. Declare it `-> float`.",
                node.lineno,
            )
        return MacroReturnStatement(
            source=_written(node, self.lines), expression=expression
        )

    def _number_value(self, node: ast.expr, what: str) -> MacroOperand:
        """`return` 과 대입의 오른쪽에 오는 숫자 식 하나. 모양을 알면 number 인지 본다."""
        operand, shape = self._number_expression(node, what)
        if shape is not None and shape is not MacroShape.number:
            raise MacroRejection(
                f"{what} has to be a number, and this is a {shape.value}. A `-> "
                f"{self._returns()}` def computes with {_NUMBER_FORMS}.",
                getattr(node, "lineno", None),
            )
        return operand

    def _number_expression(
        self, node: ast.expr, what: str
    ) -> tuple[MacroOperand, MacroShape | None]:
        """숫자 식의 한 자리. 참거짓과 숫자를 못 내는 호출을 먼저 거르고 `_operand` 에 넘긴다.

        먼저 거르는 이유는 거절 문장이다. `_operand` 의 문장은 비교의 한쪽을 말하므로,
        `return x > 1` 이나 `return find(...)` 가 거기까지 가면 엉뚱한 자리를 말한다.
        """
        line = getattr(node, "lineno", None)
        is_bool_literal = isinstance(node, ast.Constant) and type(node.value) is bool
        if is_bool_literal or isinstance(node, (ast.Compare, ast.BoolOp)) or (
            isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
        ):
            raise MacroRejection(
                f"{what} has to be a number, and this is true or false. A `-> bool` "
                "predicate is the def that returns a condition.",
                line,
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called = node.func.id
            _reject_jev_call(node)
            if called == ASK:
                self._ask(node, ask_allowed=False)
            computes = (
                called in READERS_BY_NAME
                or called in ARITHMETIC_FUNCTIONS
                or self._number_signature(called) is not None
            )
            if not computes:
                raise MacroRejection(
                    f"`{called}(...)` cannot stand in {what}: it gives no number. A "
                    f"number here is {_NUMBER_FORMS}.",
                    line,
                )
        return self._operand(node, what)

    def _number_signature(self, name: str) -> _Signature | None:
        signature = self.signatures.get(name)
        if signature is None or signature.returns not in NUMBER_RETURNS:
            return None
        return signature

    def _number_call(self, node: ast.Call) -> MacroNumberCallOperand:
        """인자는 helper·predicate 호출과 같은 규칙이다 — 리터럴·이름·target 만 받는다."""
        signature = self.signatures[node.func.id]  # type: ignore[union-attr]
        return MacroNumberCallOperand(
            callee=signature.name,
            arguments=self._arguments(signature.name, _helper_parameters(signature), node),
        )

    def _arithmetic(self, node: ast.BinOp, side: str) -> MacroArithmeticOperand:
        arithmetic = _ARITHMETIC_OPERATORS.get(type(node.op))
        if arithmetic is None:
            raise MacroRejection(
                f"that operator is not allowed. A `-> {self._returns()}` def computes "
                f"with {_ARITHMETIC_NAMES}.",
                node.lineno,
            )
        return MacroArithmeticOperand(
            left=self._arithmetic_side(node.left, arithmetic, side),
            operator=arithmetic,
            right=self._arithmetic_side(node.right, arithmetic, side),
        )

    def _negation(self, node: ast.UnaryOp, side: str) -> MacroArithmeticOperand:
        """`-x` 를 `0 - x` 로 든다. 숫자 리터럴 앞의 `-` 는 `_literal` 이 이미 받는다."""
        return MacroArithmeticOperand(
            left=MacroLiteralOperand(literal=MacroLiteral(shape=MacroShape.number, value=0)),
            operator=MacroArithmetic.subtract,
            right=self._arithmetic_side(node.operand, MacroArithmetic.subtract, side),
        )

    def _extremum(self, node: ast.Call, side: str) -> MacroExtremumOperand:
        arithmetic = ARITHMETIC_FUNCTIONS[node.func.id]  # type: ignore[union-attr]
        if node.keywords or len(node.args) < 2:
            raise MacroRejection(
                f"`{arithmetic.value}` takes two or more numbers, written as positional "
                f"arguments: `{arithmetic.value}(a, b)`.",
                node.lineno,
            )
        return MacroExtremumOperand(
            operator=arithmetic,
            operands=tuple(
                self._arithmetic_side(argument, arithmetic, side) for argument in node.args
            ),
        )

    def _arithmetic_side(
        self, node: ast.expr, arithmetic: MacroArithmetic, side: str
    ) -> MacroOperand:
        """산술 하나의 피연산자. 모양을 알면 `grammar.ARITHMETIC_TABLE` 에 묻는다.

        모양을 모르는 `member`·`static`·`observable`·`at_start` 는 값이 도착한 뒤
        `binding.calculate` 가 같은 표로 거절한다.
        """
        operand, shape = self._number_expression(node, side)
        if shape is not None and not arithmetic_allowed(shape, arithmetic):
            raise MacroRejection(
                f"`{arithmetic.value}` is not allowed on a {shape.value}. Arithmetic takes "
                f"numbers only: {_NUMBER_FORMS}.",
                getattr(node, "lineno", None),
            )
        return operand

    def _may_have_fraction(self, operand: MacroOperand) -> bool:
        """저장 때 보이는 것만으로 소수가 나올 수 있나. `-> int` 와 `int` 대입이 묻는다.

        reader 는 `False` 다. 도착한 값은 int 와 float 를 못 가르므로(`grammar.MacroShape`)
        `count: int = member(...)` 가 이미 그렇듯 실행 때도 가르지 않는다.
        """
        if isinstance(operand, MacroLiteralOperand):
            return isinstance(operand.literal.value, float)
        if isinstance(operand, MacroNameOperand):
            return operand.declared_type is MacroType.float_
        if isinstance(operand, MacroNumberCallOperand):
            return self.signatures[operand.callee].returns == MacroType.float_.value
        if isinstance(operand, MacroArithmeticOperand):
            return (
                operand.operator is MacroArithmetic.divide
                or self._may_have_fraction(operand.left)
                or self._may_have_fraction(operand.right)
            )
        if isinstance(operand, MacroExtremumOperand):
            return any(self._may_have_fraction(one) for one in operand.operands)
        return False

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
        if isinstance(node, ast.AsyncFor):
            raise MacroRejection(
                "`async for` is not allowed. Write a plain `for`.", line
            )
        if isinstance(node, (ast.Break, ast.Continue)):
            # 몸통 중간에서 빠져나가면 `pending` 과 `skipped` 의 뜻이 한 번 더 흔들린다.
            # 필요해지면 그때 연다.
            keyword = "break" if isinstance(node, ast.Break) else "continue"
            raise MacroRejection(
                f"`{keyword}` is not allowed. A loop runs its body whole on every pass. "
                "To stop early, make it a `while` whose condition turns false; to skip "
                "part of a pass, put that part under an `if`.",
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
                "`return` is allowed only inside a `-> bool` predicate. A `-> None` "
                "def acts and returns no value.",
                line,
            )
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise MacroRejection(
                f"`import` is not allowed inside a `def`. {_IMPORT_FORM}", line
            )
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            raise MacroRejection(
                "a bare string is allowed only as a docstring, which is the first "
                "statement of a `def`. Anywhere else — later in a body, or inside an "
                "`if` or a loop — write the explanation as a `#` comment.",
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

        if self._returns_number() and self._computes(node):
            return self._compute_value(node, declared, name), _COMPUTE

        if isinstance(node, ast.Name):
            # 먼저 bind 됐는지 묻는다. 소문자 `true` 가 여기로 오므로, 그 자리에서 `True` 를
            # 이름으로 대 줘야 모델이 한 번에 고친다. bind 된 이름이면 아래에서 거절한다 —
            # 이름을 다시 bind 하는 것은 재대입과 같은 것이고 macro 에 재대입은 없다.
            self._require_bound(node)
            raise MacroRejection(
                f"`{name}` is bound to `{node.id}`, which is already a name in this "
                "`def`. A macro binds a name to `find(...)`, `selector(...)`, a reader "
                f"call, or one literal — never to another name. Use `{node.id}` where you "
                "meant it.",
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
            if node.func.id in READERS_BY_NAME:
                return self._read_value(node, declared, name), _READ
            if node.func.id == ASK:
                raise MacroRejection(_ASK_ONLY_AS_CONDITION, node.lineno)
            _reject_jev_call(node)
            if node.func.id in (FIND_ALL, RANGE):
                raise MacroRejection(
                    f"`{name}` is bound to `{node.func.id}(...)`, which hands a loop "
                    "several things and stands only after `for <name> in`. A name holds "
                    "one value; to bind one object, use `find(...)`.",
                    node.lineno,
                )

        raise MacroRejection(
            f"`{name}` is bound to something a macro cannot bind. The right-hand side is "
            f"one of four things: `find(...)`, `selector(...)`, one reader call "
            f"({READER_NAME_LIST}), or one literal (an int, a float, a string or a bool). "
            "Arithmetic and any other call are refused.",
            getattr(node, "lineno", None),
        )

    def _computes(self, node: ast.expr) -> bool:
        """숫자 `def` 안에서 `MacroComputeValue` 로 드는 오른쪽인가.

        산술식, `min`·`max`, 숫자 `def` 호출이다. reader 하나와 리터럴 하나는 예전 그대로
        `MacroReadValue`·`MacroLiteralValue` 로 든다.
        """
        if isinstance(node, ast.BinOp):
            return True
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return (
                node.func.id in ARITHMETIC_FUNCTIONS
                or self._number_signature(node.func.id) is not None
            )
        return False

    def _compute_value(
        self, node: ast.expr, declared: MacroType, name: str
    ) -> MacroComputeValue:
        if declared.value not in NUMBER_RETURNS:
            raise MacroRejection(
                f"`{name}` is declared `{declared.value}`, but it is bound to a number "
                "this def computes. Declare it `int` or `float`.",
                getattr(node, "lineno", None),
            )
        expression = self._number_value(node, f"`{name}`")
        if declared is MacroType.int_ and self._may_have_fraction(expression):
            raise MacroRejection(
                f"`{name}` is declared `int`, but what it is bound to can have a fraction "
                "— it divides with `/`, or uses a float literal, a name declared `float` or "
                "a `-> float` def. Declare it `float`.",
                getattr(node, "lineno", None),
            )
        return MacroComputeValue(expression=expression)

    def _read_value(
        self, node: ast.Call, declared: MacroType, name: str
    ) -> MacroReadValue:
        """reader 호출을 대입 오른쪽으로 받는다. 값은 runner 가 그 statement 에서 읽는다.

        `object` 로 선언하면 거절한다. reader 는 값을 읽지 객체를 찾지 않으므로, 그 이름으로
        겨눌 수 있다고 받아 주면 저장 때 막을 것이 실행 때까지 간다.

        도착 모양이 정해진 reader(`scene`·`text` 는 문자열, `actionable`·`exists`·`absent`
        는 bool)는 선언한 타입과 여기서 맞춘다. `member`·`observable`·`static` 은 게임이
        모양을 정하므로 맞추는 것이 실행 때로 간다 — 비교가 이미 그렇게 한다.
        """
        spec = READERS_BY_NAME[node.func.id]  # type: ignore[union-attr]
        if declared is MacroType.object_:
            raise MacroRejection(
                f"`{name}` is declared `object` but bound to `{spec.name}(...)`, which "
                "reads a value rather than finding an object. Declare it `int`, `float`, "
                "`string` or `bool`. To aim at something, bind `find(...)` or "
                "`selector(...)` instead.",
                node.lineno,
            )
        call = self._reader(node)
        wanted = _DECLARED_FOR_SHAPE.get(spec.shape) if spec.shape is not None else None
        if wanted is not None and declared not in wanted:
            names = " or ".join(f"`{one.value}`" for one in wanted)
            raise MacroRejection(
                f"`{name}` is declared `{declared.value}`, but `{spec.name}(...)` always "
                f"arrives as a {spec.shape.value}. Declare `{name}` as {names}.",
                node.lineno,
            )
        return MacroReadValue(call=call)

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

    def _find(self, node: ast.Call, called: str = FIND) -> MacroFindValue:
        if node.args:
            raise MacroRejection(
                f"`{called}` takes keyword arguments only: "
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
                    f"`**` is not allowed in a `{called}` call.", node.lineno
                )
            if keyword.arg not in FIND_KEYWORDS:
                raise MacroRejection(
                    f"`{called}` has no `{keyword.arg}=` argument. It takes "
                    f"{', '.join(f'{name}=' for name in FIND_KEYWORDS)}.",
                    node.lineno,
                )
            if keyword.arg in given:
                raise MacroRejection(
                    f"`{keyword.arg}=` is given twice in this `{called}` call.", node.lineno
                )
            given[keyword.arg] = self._string_ref(
                keyword.value, f"`{keyword.arg}=` in `{called}`"
            )

        if not any(keyword in given for keyword in FIND_REQUIRED_KEYWORDS):
            raise MacroRejection(
                f"`{called}` needs at least one of `label=` or `name=`. `under=` only narrows "
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
        condition = self._condition(node.test, ask_allowed=True)
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

    # -- 반복 --

    def _for(self, node: ast.For) -> MacroForStatement:
        """`for <name> in find_all(...)` 이거나 `for <name> in range(n)`.

        이름의 타입은 도는 대상이 정한다. Python 의 `for` 는 이름에 타입을 못 적으므로
        여기서 정해 둔다 — `find_all` 이면 `object`, `range` 이면 `int`.
        """
        if node.orelse:
            raise MacroRejection(
                "`for ... else` is not allowed. Put what should follow the loop after "
                "it, unindented.",
                node.lineno,
            )
        if not isinstance(node.target, ast.Name):
            raise MacroRejection(
                "a `for` binds one bare name per pass. Unpacking (`for a, b in ...`) is "
                "not allowed.",
                node.lineno,
            )
        iterable, declared = self._iterable(node.iter)
        body = self.body(node.body, node, loop_name=(node.target.id, declared))
        return MacroForStatement(
            source=_written(node, self.lines, header_only=True),
            name=node.target.id,
            declared_type=declared,
            iterable=iterable,
            body=body,
        )

    def _iterable(self, node: ast.expr) -> tuple[MacroIterable, MacroType]:
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in (FIND_ALL, RANGE)
        ):
            raise MacroRejection(
                "a `for` walks over one of two things: `find_all(label=..., name=..., "
                "under=...)`, which hands it every object that matches, or `range(n)`, "
                "which counts n passes. A list or any other call is not allowed.",
                getattr(node, "lineno", None),
            )
        if node.func.id == FIND_ALL:
            return MacroFindAllIterable(find=self._find(node, FIND_ALL)), MacroType.object_
        return self._range(node), MacroType.int_

    def _range(self, node: ast.Call) -> MacroRangeIterable:
        if node.keywords or len(node.args) != 1:
            raise MacroRejection(
                "`range` takes exactly one argument here: how many passes, as a whole "
                "number or a name declared `int`. Start and step are not allowed.",
                node.lineno,
            )
        argument = node.args[0]
        literal = _literal(argument)
        if literal is not None:
            if literal.shape is not MacroShape.number or not isinstance(literal.value, int):
                raise MacroRejection(
                    "`range` counts passes, so its argument is a whole number.",
                    node.lineno,
                )
            if not 0 <= literal.value <= MAX_LOOP_PASSES:
                raise MacroRejection(
                    f"`range({literal.value})` asks for {literal.value} passes, and a "
                    f"loop turns at most {MAX_LOOP_PASSES} times. Split the work, or "
                    "observe the screen between macros.",
                    node.lineno,
                )
            return MacroRangeIterable(count=literal)
        if isinstance(argument, ast.Name):
            bound = self._require_bound(argument)
            if bound.declared_type is not MacroType.int_:
                raise MacroRejection(
                    f"`range({argument.id})` needs `{argument.id}` declared `int`; it is "
                    f"declared `{bound.declared_type.value}`.",
                    node.lineno,
                )
            return MacroRangeIterable(name=argument.id)
        raise MacroRejection(
            "`range` takes a whole number or a name declared `int`. Arithmetic is not "
            "allowed.",
            node.lineno,
        )

    def _while(self, node: ast.While) -> MacroWhileStatement:
        """조건은 `if` 와 같은 문법이고 runner 가 매 회 전에 다시 읽는다."""
        if node.orelse:
            raise MacroRejection(
                "`while ... else` is not allowed. Put what should follow the loop after "
                "it, unindented.",
                node.lineno,
            )
        condition = self._condition(node.test, ask_allowed=True)
        body = self.body(node.body, node)
        return MacroWhileStatement(
            source=_written(node, self.lines, header_only=True),
            condition=condition,
            body=body,
        )

    # -- `match decide(...)` --

    def _match(self, node: ast.Match) -> MacroMatchStatement:
        """`match decide("<question>", key=value, ...):` 와 가져온 macro 이름의 `case` 들.

        `case` 몸통은 `if` 몸통과 같은 규칙이다. 각자 자기 scope 를 받고, 그 안에서 bind 한
        이름은 그 `case` 와 함께 끝난다.
        """
        subject = node.subject
        if not (
            isinstance(subject, ast.Call)
            and isinstance(subject.func, ast.Name)
            and subject.func.id == DECIDE
        ):
            raise MacroRejection(
                "a `match` takes exactly one subject: "
                '`decide("<question>", key=value, ...)`. Each `case` is a string naming '
                "an imported macro, and Jev picks one.",
                node.lineno,
            )
        question, context = self._jev_call(subject, DECIDE)

        cases: list[MacroMatchCase] = []
        for case in node.cases:
            option = self._case_option(case)
            if any(existing.option == option for existing in cases):
                raise MacroRejection(
                    f'`case "{option}"` is written twice. One option is one case.',
                    case.pattern.lineno,
                )
            cases.append(MacroMatchCase(option=option, body=self.body(case.body, case.pattern)))

        if len(cases) < MIN_MATCH_CASES:
            raise MacroRejection(
                f"`match decide(...)` needs at least {MIN_MATCH_CASES} cases; with one "
                "option there is nothing to decide. Call that macro directly instead.",
                node.lineno,
            )
        return MacroMatchStatement(
            source=_written(node, self.lines, header_only=True),
            decide=MacroDecideCall(question=question, context=context),
            cases=tuple(cases),
        )

    def _case_option(self, case: ast.match_case) -> str:
        line = case.pattern.lineno
        imported = ", ".join(self.imported) or "none"
        if case.guard is not None:
            raise MacroRejection(
                "a `case` guard (`case ... if ...`) is not allowed. Jev picks the case; "
                "put a condition inside the case body as an `if`.",
                line,
            )
        pattern = case.pattern
        if not (
            isinstance(pattern, ast.MatchValue)
            and isinstance(pattern.value, ast.Constant)
            and isinstance(pattern.value.value, str)
        ):
            raise MacroRejection(
                "each `case` is one string naming an imported macro, e.g. "
                '`case "end_turn":`. `case _`, a capture name, and any other pattern are '
                f"not allowed. Imported here: {imported}.",
                line,
            )
        option = pattern.value.value
        if option == NONE_FITS:
            raise MacroRejection(
                f'`case "{NONE_FITS}"` is not allowed: Jev is always offered '
                f"`{NONE_FITS}`, and when it picks it the macro pauses and hands the "
                "turn back to you. Name an imported macro.",
                line,
            )
        if option not in self.imported:
            raise MacroRejection(
                f'`case "{option}"` does not name an imported macro. The option Jev reads '
                "is that macro's docstring, so each case names one macro imported with "
                f"`from {IMPORT_MODULE} import ...`. Imported here: {imported}.",
                line,
            )
        return option

    # -- Jev 에게 묻는 호출 --

    def _jev_call(
        self, node: ast.Call, called: str
    ) -> tuple[str, tuple[MacroContextArgument, ...]]:
        """`decide(...)` 와 `ask(...)` 가 함께 쓰는 모양: 질문 하나와 keyword context."""
        if len(node.args) != 1:
            raise MacroRejection(
                f'`{called}` takes exactly one positional argument, the question as a '
                f'plain string, then keyword context: `{called}("<question>", '
                "key=value, ...)`.",
                node.lineno,
            )
        question_node = node.args[0]
        if isinstance(question_node, ast.JoinedStr):
            raise MacroRejection(
                f"the question of `{called}` cannot be an f-string. Write it out, and "
                "pass the values Jev should read as keyword context.",
                node.lineno,
            )
        question = _string_literal(question_node, f"the question of `{called}`").strip()
        if not question:
            raise MacroRejection(
                f"the question of `{called}` cannot be empty. Ask what Jev should "
                "judge, in one sentence.",
                node.lineno,
            )
        context = tuple(
            self._context_argument(keyword, called) for keyword in node.keywords
        )
        return question, context

    def _context_argument(
        self, keyword: ast.keyword, called: str
    ) -> MacroContextArgument:
        value = keyword.value
        line = getattr(value, "lineno", None)
        if keyword.arg is None:
            raise MacroRejection(
                f"`**` is not allowed in `{called}`. Write each context value as its "
                "own `key=value`.",
                line,
            )
        key = keyword.arg
        if key in RESERVED_CONTEXT_KEYS:
            reserved = ", ".join(f"`{one}`" for one in sorted(RESERVED_CONTEXT_KEYS))
            raise MacroRejection(
                f"`{key}=` cannot be used in `{called}`: the macro always adds {reserved} "
                "to what Jev reads. Pick another key.",
                line,
            )

        literal = _literal(value)
        if literal is not None:
            return MacroContextLiteral(key=key, literal=literal)
        if isinstance(value, ast.Name):
            bound = self._require_bound(value)
            if bound.declared_type is MacroType.object_:
                return MacroContextTarget(key=key, target=MacroNameTarget(name=value.id))
            return MacroContextName(
                key=key, name=value.id, declared_type=bound.declared_type
            )
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id == SELECTOR:
                return MacroContextTarget(
                    key=key,
                    target=MacroSelectorTarget(selector=self._selector_string(value)),
                )
            if value.func.id == FIND_ALL:
                return MacroContextFindAll(key=key, find=self._find(value, FIND_ALL))
            if value.func.id == UNDER:
                return MacroContextUnder(key=key, root=self._under_root(value, key, called))
        if isinstance(value, ast.JoinedStr):
            raise MacroRejection(
                f"`{key}=` in `{called}` is an f-string, which is not allowed. Pass "
                "each value as its own key instead.",
                line,
            )
        raise MacroRejection(
            f"`{key}=` in `{called}` is not something Jev can be shown. A context value "
            f"is one of six things: {_CONTEXT_FORMS}.",
            line,
        )

    def _under_root(self, node: ast.Call, key: str, called: str) -> MacroTarget:
        """`under(...)` 의 인자 하나. 경로 문자열이거나 `object` 로 선언된 이름이다.

        경로는 앞뒤의 `/` 를 떼고 든다. runner 가 그 경로와 같거나 `<경로>/` 로 시작하는
        object 를 고르므로, `Hand` 가 `HandHint` 까지 잡지 않는다.
        """
        usage = (
            f'`under` in `{key}=` of `{called}` takes one thing: a path string such as '
            '`under("DebugCanvas[4]/Hand[1]")`, or a name declared `object`.'
        )
        if node.keywords or len(node.args) != 1:
            raise MacroRejection(usage, node.lineno)
        argument = node.args[0]
        literal = _literal(argument)
        if literal is not None:
            path = ""
            if literal.shape is MacroShape.string:
                path = str(literal.value).strip().strip("/")
            if not path:
                raise MacroRejection(usage, node.lineno)
            return MacroSelectorTarget(selector=path)
        if isinstance(argument, ast.Name):
            bound = self._require_bound(argument)
            if bound.declared_type is not MacroType.object_:
                raise MacroRejection(
                    f"`{argument.id}` is declared `{bound.declared_type.value}`, not "
                    f"`object`. {usage}",
                    node.lineno,
                )
            return MacroNameTarget(name=argument.id)
        raise MacroRejection(usage, node.lineno)

    def _checkpoint(self, node: ast.Call, written: str) -> MacroCheckpointStatement:
        if node.keywords or len(node.args) > 1:
            raise MacroRejection(
                "`checkpoint` takes nothing, or one string saying what to look at here.",
                node.lineno,
            )
        reason = ""
        if node.args:
            reason = _string_literal(node.args[0], "the reason of `checkpoint`").strip()
            if not reason:
                raise MacroRejection(
                    "the reason of `checkpoint` cannot be empty. Say what to look at "
                    "before going on, or write `checkpoint()` with no argument.",
                    node.lineno,
                )
        return MacroCheckpointStatement(source=written, reason=reason)

    # -- 조건 --

    def _condition(self, node: ast.expr, ask_allowed: bool = False) -> MacroCondition:
        """`require` 의 첫 인자와 `if` 의 조건이 같은 문법을 쓴다.

        평가기가 하나이기 때문이다. 그래서 `and`·`or`·`not` 은 `if` 의 조건에서도
        거절이고, 평가 시점의 연산자 거절도 한 곳에서 난다.

        `ask_allowed` 는 `if`·`elif`·`while`·`require` 세 자리만 켠다. `ask(...)` 는 조건
        **전체**로만 서므로, 부모 node 를 거슬러 보지 않고 부르는 쪽이 알려 준다.
        """
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == ASK:
                return self._ask(node, ask_allowed)
            _reject_jev_call(node)
        if isinstance(node, ast.BoolOp):
            joiner = "and" if isinstance(node.op, ast.And) else "or"
            raise MacroRejection(
                f"`{joiner}` is not allowed in a condition. One condition is one "
                "comparison, one reader call, or one predicate call. Write a `-> bool` "
                "predicate with `if` and `return`, or use two `require` calls so each "
                "failure has its own remedy.",
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
            if node.func.id in self.signatures:
                signature = self.signatures[node.func.id]
                if signature.returns in NUMBER_RETURNS:
                    raise MacroRejection(
                        f"`{signature.name}` is declared `-> {signature.returns}` and "
                        "returns a number, so it is not a condition on its own. Compare "
                        f"it: `{signature.name}(...) >= <number>`.",
                        node.lineno,
                    )
                if signature.returns != "bool":
                    raise MacroRejection(
                        f"`{signature.name}` is declared `-> None` and cannot be a "
                        "condition. Call a `-> bool` predicate here.",
                        node.lineno,
                    )
                return MacroPredicateCondition(
                    callee=signature.name,
                    arguments=self._arguments(
                        signature.name, _helper_parameters(signature), node
                    ),
                )
            if node.func.id in self.imported:
                raise MacroRejection(
                    f"`{node.func.id}` is an imported macro: it acts and returns nothing, "
                    "so it cannot be a condition. Call it on its own line, or call a "
                    "`-> bool` predicate here.",
                    node.lineno,
                )
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
                f"with are: {READER_NAME_LIST}, or a `-> bool` predicate call.",
                node.lineno,
            )
        raise MacroRejection(
            "a condition is one comparison (`==`, `!=`, `>`, `<`, `>=`, `<=`) or one "
            f"reader call ({READER_NAME_LIST}) or a `-> bool` predicate call.",
            getattr(node, "lineno", None),
        )

    def _ask(self, node: ast.Call, ask_allowed: bool) -> MacroAskCondition:
        if self._is_predicate():
            raise MacroRejection(
                "`ask(...)` is not allowed inside a `-> bool` predicate, its `return` "
                "included: predicates are evaluated synchronously and `ask` waits for "
                f"Jev. {_ASK_ONLY_AS_CONDITION}",
                node.lineno,
            )
        if self._returns_number():
            raise MacroRejection(
                f"`ask(...)` is not allowed inside a `-> {self._returns()}` def: a def "
                "that returns a number is evaluated synchronously and `ask` waits for "
                f"Jev. {_ASK_ONLY_AS_CONDITION}",
                node.lineno,
            )
        if not ask_allowed:
            raise MacroRejection(_ASK_ONLY_AS_CONDITION, node.lineno)
        question, context = self._jev_call(node, ASK)
        return MacroAskCondition(question=question, context=context)

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

        # 숫자 `def` 안에서는 산술식이 피연산자로 선다. 밖에서는 아래의 예전 거절 그대로다.
        if self._returns_number():
            if isinstance(node, ast.BinOp):
                return self._arithmetic(node, side), MacroShape.number
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
                return self._negation(node, side), MacroShape.number
            if isinstance(node, ast.Call) and _is_call_to_any(node, ARITHMETIC_FUNCTIONS):
                return self._extremum(node, side), MacroShape.number

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == ASK:
                raise MacroRejection(
                    f"`ask(...)` cannot stand on {side} of a comparison; it is already "
                    f"true or false. {_ASK_ONLY_AS_CONDITION}",
                    node.lineno,
                )
            _reject_jev_call(node)
            # 숫자 `def` 호출은 비교의 한쪽 어디에나 선다 — `-> None` 의 `if`·`while`·
            # `require`·`wait_until` 과 predicate 의 `return` 까지.
            if self._number_signature(node.func.id) is not None:
                return self._number_call(node), MacroShape.number
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
        # `at_start("Type.member")` 처럼 target 을 뺀 모양이다. 그때는 key 하나만 읽는다.
        without_target = spec.target_optional and len(node.args) == wanted - 1
        if len(node.args) != wanted and not without_target:
            raise MacroRejection(
                f"`{spec.name}` takes {_reader_arguments(spec)}.", node.lineno
            )

        index = 0
        target: MacroTarget | None = None
        if spec.takes_target and not without_target:
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
        if name == WAIT_UNTIL:
            return self._wait_until(node, written)
        if name == FLAG:
            return self._flag(node, written)
        if name == ASK_VERDICT:
            return self._ask_verdict(node, written)
        if name == CHECKPOINT:
            return self._checkpoint(node, written)
        if name in (FIND_ALL, RANGE):
            raise MacroRejection(
                f"`{name}(...)` stands only after `for <name> in`. It hands the loop "
                "the things to walk over; on its own line it does nothing.",
                node.lineno,
            )
        if name == ASK:
            raise MacroRejection(_ASK_ONLY_AS_CONDITION, node.lineno)
        _reject_jev_call(node)

        if name in TOOLS_BY_NAME:
            spec = TOOLS_BY_NAME[name]
            return MacroActionStatement(
                source=written,
                callee=name,
                arguments=self._arguments(spec.name, spec.parameters, node),
            )

        if name in self.signatures:
            helper = self.signatures[name]
            if helper.returns == "bool":
                raise MacroRejection(
                    f"`{name}` is a predicate and returns a value; call it in a "
                    "condition.",
                    node.lineno,
                )
            if helper.returns in NUMBER_RETURNS:
                raise MacroRejection(
                    f"`{name}` is declared `-> {helper.returns}` and returns a number; "
                    f"compare it in a condition: `if {name}(...) >= <number>:`.",
                    node.lineno,
                )
            return MacroHelperCallStatement(
                source=written,
                callee=name,
                arguments=self._arguments(
                    helper.name, _helper_parameters(helper), node
                ),
            )

        if name in self.imported:
            # 가져온 macro 도 helper 와 같은 statement 다. 인자는 그 macro 의 entry
            # parameter 에 맞춰 bind 한다.
            imported = self.imported[name]
            return MacroHelperCallStatement(
                source=written,
                callee=name,
                arguments=self._arguments(
                    imported.name, _helper_parameters(imported), node
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
        imported = ", ".join(self.imported) or "none"
        raise MacroRejection(
            f"`{name}` is not something a macro can call. The game tools are: "
            f"{TOOL_NAME_LIST}. Other statements are `require()`, `wait_until()`, "
            "`flag()`, `ask_verdict()`, and `checkpoint()`. The `def` names in this "
            f"source are: {helpers}. The macros imported with `from {IMPORT_MODULE} "
            f"import ...` are: {imported}.",
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
        condition = self._condition(node.args[0], ask_allowed=True)
        remedy = _string_literal(node.args[1], "the remedy of `require`").strip()
        if not remedy:
            raise MacroRejection(
                "the remedy of `require` cannot be empty. Write what to do when the "
                "condition is false, in one sentence.",
                node.lineno,
            )
        return MacroRequireStatement(source=written, condition=condition, remedy=remedy)

    def _wait_until(self, node: ast.Call, written: str) -> MacroWaitUntilStatement:
        if node.keywords:
            first = node.keywords[0].arg or "**"
            raise MacroRejection(
                f"`{first} = ...` inside `wait_until` is a keyword argument, not a "
                "comparison. Use three positional arguments and write `==` to "
                "compare values: `wait_until(<condition>, <seconds>, \"<remedy>\")`.",
                node.lineno,
            )
        if len(node.args) != 3:
            raise MacroRejection(
                "`wait_until` takes exactly three positional arguments: a condition, "
                "seconds, and a non-empty remedy string.",
                node.lineno,
            )
        if _is_call_to(node.args[0], ASK):
            raise MacroRejection(
                "`ask(...)` is not allowed inside `wait_until`: the condition is read "
                "again on every pulse, so it would call Jev on every pulse. Use "
                "`ask(...)` as the condition of an `if`, `while` or `require`, and wait "
                "for a reader condition here.",
                node.lineno,
            )
        condition = self._condition(node.args[0])
        seconds_literal = _literal(node.args[1])
        if seconds_literal is None or type(seconds_literal.value) not in (int, float):
            raise MacroRejection(
                "the seconds of `wait_until` must be an int or float literal, not a "
                "bool or name. The limit must be checked when the macro is stored.",
                node.lineno,
            )
        seconds = float(seconds_literal.value)
        if not 0 < seconds <= MAX_WAIT_PER_STATEMENT_SECONDS:
            raise MacroRejection(
                f"the seconds of `wait_until` must be greater than 0 and at most "
                f"{MAX_WAIT_PER_STATEMENT_SECONDS}.",
                node.lineno,
            )
        remedy = _string_literal(node.args[2], "the remedy of `wait_until`").strip()
        if not remedy:
            raise MacroRejection(
                "the remedy of `wait_until` cannot be empty. Write what to do if "
                "the condition remains false.",
                node.lineno,
            )
        return MacroWaitUntilStatement(
            source=written, condition=condition, seconds=seconds, remedy=remedy
        )

    def _flag(self, node: ast.Call, written: str) -> MacroFlagStatement:
        if node.keywords or len(node.args) != 1:
            raise MacroRejection(
                "`flag` takes exactly one argument: a message written out as a string. "
                "It only tells the agent what the macro saw — it names no step and sets "
                "no obligation. `ask_verdict(<expected>)` is the one that does.",
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
        # step 번호는 적지 않는다. `run_macro` 호출이 받은 step 을 안쪽 statement 가 전부
        # 물려받는데, 여기 번호를 따로 적게 하면 그 번호가 호출의 step 과 어긋날 자리가
        # 생긴다. 실제로 어긋났다 — `ask_verdict(4, ...)` 로 등록한 macro 를
        # `run_macro(step=7)` 로 부르면 agent 에게 4번 step 을 판정하라고 말했다.
        if not node.keywords and len(node.args) == 2 and _literal(node.args[0]) is not None:
            raise MacroRejection(
                "`ask_verdict` does not take a step number. The step is the one "
                "`run_macro` was called with, so a number here could only disagree with "
                "it. Write `ask_verdict(<expected>)` with the expectation alone.",
                node.lineno,
            )
        if node.keywords or len(node.args) != 1:
            raise MacroRejection(
                "`ask_verdict` takes exactly one argument: what should be true at this "
                "step, written out as a string. The step is the one `run_macro` was "
                "called with.",
                node.lineno,
            )
        expected = _string_literal(
            node.args[0], "the expectation of `ask_verdict`"
        ).strip()
        if not expected:
            raise MacroRejection(
                "the expectation of `ask_verdict` cannot be empty. Write what should be "
                "true at that step — a sentence that is true whenever the macro runs, "
                "not what you happened to see on one run.",
                node.lineno,
            )
        return MacroAskVerdictStatement(source=written, expected=expected)

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


def _is_call_to(node: ast.expr, called: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == called
    )


def _is_call_to_any(node: ast.expr, called: Mapping[str, object]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in called
    )


def _reject_jev_call(node: ast.Call) -> None:
    """`match` 의 주어가 아닌 자리에 온 `decide(...)` 를 거절한다."""
    if _is_call_to(node, DECIDE):
        raise MacroRejection(_DECIDE_ONLY_IN_MATCH, node.lineno)


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
    if spec.target_optional:
        return f"one plain string, or {' and '.join(parts)}"
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


def _number_callees(operand: MacroOperand) -> list[str]:
    """숫자 식 하나가 부르는 숫자 `def` 이름, 적힌 순서대로."""
    if isinstance(operand, MacroNumberCallOperand):
        return [operand.callee]
    if isinstance(operand, MacroArithmeticOperand):
        return _number_callees(operand.left) + _number_callees(operand.right)
    if isinstance(operand, MacroExtremumOperand):
        return [name for one in operand.operands for name in _number_callees(one)]
    return []


def _condition_callees(condition: MacroCondition) -> list[str]:
    if isinstance(condition, MacroPredicateCondition):
        return [condition.callee]
    if isinstance(condition, MacroComparisonCondition):
        return _number_callees(condition.left) + _number_callees(condition.right)
    return []


def _expression_callees(statement: MacroStatement) -> list[str]:
    """statement 가 조건·`return`·대입 안에서 부르는 predicate 와 숫자 `def`.

    숫자 `def` 가 생기기 전에는 조건 하나가 predicate 하나만 부를 수 있었다. 이제 비교의
    양쪽과 산술식 안에서 여럿을 부를 수 있어 목록이다.
    """
    names: list[str] = []
    if (
        isinstance(
            statement,
            (
                MacroRequireStatement,
                MacroIfStatement,
                MacroWhileStatement,
                MacroWaitUntilStatement,
                MacroReturnStatement,
            ),
        )
        and statement.condition is not None
    ):
        names.extend(_condition_callees(statement.condition))
    if isinstance(statement, MacroReturnStatement) and statement.expression is not None:
        names.extend(_number_callees(statement.expression))
    if isinstance(statement, MacroAssignStatement) and isinstance(
        statement.value, MacroComputeValue
    ):
        names.extend(_number_callees(statement.value.expression))
    return names


def _called_helpers(statements: tuple[MacroStatement, ...]) -> list[str]:
    """그 몸통이 부르는 helper·predicate·숫자 `def` 이름 전부, 부른 순서대로.

    분기 안쪽까지 센다. 같은 `def` 를 두 번 부르면 두 번 적힌다. 세는 쪽이 그것을
    알아야 statement 총수가 맞는다.
    """
    names: list[str] = []
    for statement in statements:
        names.extend(_expression_callees(statement))
        if isinstance(statement, MacroHelperCallStatement):
            names.append(statement.callee)
        elif isinstance(statement, MacroIfStatement):
            names.extend(_called_helpers(statement.body))
            names.extend(_called_helpers(statement.orelse))
        elif isinstance(statement, (MacroForStatement, MacroWhileStatement)):
            names.extend(_called_helpers(statement.body))
        elif isinstance(statement, MacroMatchStatement):
            for case in statement.cases:
                names.extend(_called_helpers(case.body))
    return names


class _CallGraph:
    """`def` 들이 서로를 부르는 모양. 네 검사가 여기에 묻는다.

    답을 전부 memo 한다. 그래프는 저장 시점에 고정이고 질문은 되풀이되므로, 같은 답을
    두 번 세는 것은 그대로 지수가 된다.

    `from macros import ...` 로 가져온 macro 는 이 그래프에서 node 하나다. 그 안쪽은 그
    macro 자신의 정의로 세운 그래프가 답한다(`_owner`). 그래서 두 macro 의 helper 이름이
    겹쳐도 섞이지 않고, 가져온 macro 의 깊이·statement 수·카운터가 부르는 쪽에 그대로 더해진다.
    가져온 정의는 이미 파싱된 것이라 부르는 쪽을 다시 가리킬 수 없다.
    """

    def __init__(self, definition: MacroDefinition) -> None:
        self.definition = definition
        self.calls: dict[str, list[str]] = {
            definition.entry.name: _called_helpers(definition.entry.statements)
        }
        for helper in definition.helpers:
            self.calls[helper.name] = _called_helpers(helper.statements)
        self.imported: dict[str, _CallGraph] = {
            imported.name: _CallGraph(imported) for imported in definition.imports
        }
        self._depth: dict[str, int] = {}
        self._statements: dict[str, int] = {}
        self._tally: dict[tuple[str, str], _Tally] = {}
        self._holding_walked: set[tuple[str, tuple[int, ...]]] = set()

    def _owner(self, name: str) -> "tuple[_CallGraph, str]":
        """그 이름을 답할 그래프와, 그 그래프 안에서의 이름.

        가져온 macro 면 그 macro 의 그래프와 그 entry 이름이고, 아니면 이 그래프와 그 이름이다.
        """
        imported = self.imported.get(name)
        if imported is not None:
            return imported, imported.definition.entry.name
        return self, name

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
        owner, own_name = self._owner(name)
        if owner is not self:
            return owner.depth_of(own_name)
        if name not in self._depth:
            called = self.calls.get(name, ())
            # 먼저 넣어 두지 않는다. 고리는 `reject_recursion` 이 이미 걸렀으므로 여기
            # 닿는 그래프는 비순환이다.
            self._depth[name] = 1 + max(
                (self.depth_of(one) for one in called), default=0
            )
        return self._depth[name]

    def deepest_chain(self, name: str) -> list[str]:
        """`depth_of` 를 낸 가장 깊은 호출 사슬. 거절 문장이 그 사슬을 그대로 적는다."""
        owner, own_name = self._owner(name)
        if owner is not self:
            return owner.deepest_chain(own_name)
        called = self.calls.get(name, ())
        if not called:
            return [name]
        deepest = max(called, key=self.depth_of)
        return [name] + self.deepest_chain(deepest)

    def reject_over_depth(self) -> None:
        measured = self.depth_of(self.definition.entry.name)
        if measured > MAX_CALL_DEPTH:
            chain = " → ".join(self.deepest_chain(self.definition.entry.name))
            raise MacroRejection(
                f"the call chain in this macro is {measured} deep ({chain}), and the "
                f"limit is {MAX_CALL_DEPTH} counting the entry point. An imported macro "
                "counts its own depth. Flatten a helper into its caller."
            )

    # -- statement 총수 --

    def statements_in(self, name: str) -> int:
        owner, own_name = self._owner(name)
        if owner is not self:
            return owner.statements_in(own_name)
        if name not in self._statements:
            self._statements[name] = self._count(self.statements_of(name))
        return self._statements[name]

    def _count(self, statements: tuple[MacroStatement, ...]) -> int:
        """그 몸통에서 가장 많이 도는 경로의 **적힌** statement 수.

        `if` 는 적힌 statement 를 세는 것을 바꾸지 않는다 — 최대값은 가지들 중 큰 쪽이라
        여전히 정적이다. helper 호출은 호출 그 자체 하나에 그 helper 의 총수를 더한다.
        조건에서 부른 predicate 와 식에서 부른 숫자 `def` 의 총수도 해당 statement 에 더한다.

        **반복 몸통은 한 번만 센다.** 몇 번 돌지는 저장 때 모른다. 그래서 이 수는 실행될
        양이 아니라 적힌 길이의 상한이 됐고, 실행된 양은 runner 가 `MAX_LOOP_PASSES` 와
        `MAX_EXECUTED_STATEMENTS` 로 실행 시점에 센다(ARTEL-948). 앞서 이 자리의 근거였던
        "저장 때 잡는 것이 언제나 낫다" 는 반복이 들어오면 성립하지 않는다 — 셀 수가 없다.

        세는 것만 편다, 실행은 안 편다. 상한을 세려고 호출 트리를 펴는 것이지 실행할 때
        펴는 것이 아니다.
        """
        total = 0
        for statement in statements:
            for callee in _expression_callees(statement):
                total += self.statements_in(callee)
            if isinstance(statement, MacroIfStatement):
                total += 1 + max(
                    self._count(statement.body), self._count(statement.orelse)
                )
            elif isinstance(statement, (MacroForStatement, MacroWhileStatement)):
                total += 1 + self._count(statement.body)
            elif isinstance(statement, MacroMatchStatement):
                total += 1 + max(self._count(case.body) for case in statement.cases)
            elif isinstance(statement, MacroHelperCallStatement):
                total += 1 + self.statements_in(statement.callee)
            else:
                total += 1
        return total

    def reject_over_statements(self) -> None:
        measured = self.statements_in(self.definition.entry.name)
        if measured > MAX_STATEMENTS:
            raise MacroRejection(
                f"the longest path through this macro is {measured} statements long, "
                f"counting each loop body once, and the limit is {MAX_STATEMENTS}. "
                "Split the macro, or drop statements from the heaviest branch."
            )

    # -- 눌렀으면 풀어야 하는 tool --

    def tally_of(self, name: str, pair: PairedTools) -> "_Tally":
        owner, own_name = self._owner(name)
        if owner is not self:
            return owner.tally_of(own_name, pair)
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
            elif isinstance(statement, (MacroForStatement, MacroWhileStatement)):
                # 한 회가 0 이면 몇 회를 돌든 끝에 0 이다. 그래서 이 규칙 하나로 반복이
                # 있어도 저장 때 판정이 선다.
                one_pass = self._tally_body(statement.body, pair)
                if one_pass.delta != 0:
                    raise MacroRejection(
                        f"one pass of `{statement.source}` leaves {pair.counter} at "
                        f"{one_pass.delta:+d}. Hold and release it inside the same pass "
                        "— a loop runs its body again and again, so a pass that presses "
                        "without letting go presses once more on every turn."
                    )
                inner = _Tally(delta=0, lowest=one_pass.lowest)
            elif isinstance(statement, MacroMatchStatement):
                inner = self._tally_match(statement, pair)
            else:
                continue
            lowest = min(lowest, delta + inner.lowest)
            delta += inner.delta
        return _Tally(delta=delta, lowest=lowest)

    def _tally_match(self, statement: MacroMatchStatement, pair: PairedTools) -> "_Tally":
        """`case` 하나하나를 `else` 없는 `if` 의 몸통처럼 센다.

        어느 `case` 도 돌지 않는 경로가 언제나 있다 — Jev 가 `NONE_FITS` 를 고르고 agent 가
        `resume_macro(proceed=true)` 로 그 `match` 를 건너뛰는 경로다. 그 경로의 카운터는
        0 이므로 모든 `case` 가 0 을 남겨야 한다.
        """
        lowest = 0
        for case in statement.cases:
            one_case = self._tally_body(case.body, pair)
            if one_case.delta != 0:
                raise MacroRejection(
                    f'`case "{case.option}"` of `{statement.source}` leaves '
                    f"{pair.counter} at {one_case.delta:+d}. Hold and release it inside "
                    "the same case — Jev may pick another case, or none, and the game "
                    "would be left holding it."
                )
            lowest = min(lowest, one_case.lowest)
        return _Tally(delta=0, lowest=lowest)

    def reject_checkpoint_while_holding(self) -> None:
        """누름과 뗌 사이의 `checkpoint`·`match`, 멈춘 시간 안의 `wait_until` 을 거절한다.

        `match` 는 Jev 가 `NONE_FITS` 를 고르면 `checkpoint` 처럼 멈추므로 같은 자리다.
        가져온 macro 안은 그 macro 의 그래프가 같은 카운터 값을 들고 걷는다.

        거기서 턴이 끝나면 게임이 키를 누른 채로, 또는 시간이 멈춘 채로 턴을 넘긴다.
        `reject_unpaired` 는 macro 전체가 0 인지만 보므로 이것을 못 잡는다. 그리고 이
        규칙 덕에 멈춘 macro 를 버려도 게임에 눌린 채로 남는 것이 없다.

        `if` 의 두 가지와 반복 한 회가 같은 값을 남긴다는 것이 이미 판정됐으므로, 각
        statement 앞의 카운터 값이 경로와 상관없이 하나로 정해진다. 그 값을 진입점부터
        내려가며 들고 간다.
        """
        self._walk_holding(
            self.statements_of(self.definition.entry.name), (0,) * len(PAIRED_TOOLS)
        )

    def _walk_holding(
        self, statements: tuple[MacroStatement, ...], levels: tuple[int, ...]
    ) -> tuple[int, ...]:
        for statement in statements:
            if isinstance(statement, (MacroCheckpointStatement, MacroMatchStatement)):
                self._reject_pause_while_holding(statement, levels)
            if isinstance(statement, MacroWaitUntilStatement):
                for pair, level in zip(PAIRED_TOOLS, levels):
                    if pair.opens == "pause_game_time" and level != 0:
                        raise MacroRejection(
                            f"`{statement.source}` runs between `pause_game_time` "
                            "and `resume_game_time`. Game state cannot change while "
                            "time is frozen, so this wait would always time out. "
                            "Move `wait_until` after `resume_game_time`."
                        )
            elif isinstance(statement, MacroActionStatement):
                levels = tuple(
                    level
                    + (1 if statement.callee == pair.opens else 0)
                    - (1 if statement.callee == pair.closes else 0)
                    for pair, level in zip(PAIRED_TOOLS, levels)
                )
            elif isinstance(statement, MacroIfStatement):
                self._walk_holding(statement.orelse, levels)
                levels = self._walk_holding(statement.body, levels)
            elif isinstance(statement, (MacroForStatement, MacroWhileStatement)):
                self._walk_holding(statement.body, levels)
            elif isinstance(statement, MacroMatchStatement):
                # 모든 `case` 가 0 을 남긴다는 것이 `_tally_match` 에서 이미 판정됐다.
                for case in statement.cases:
                    self._walk_holding(case.body, levels)
            elif isinstance(statement, MacroHelperCallStatement):
                owner, own_name = self._owner(statement.callee)
                owner._enter_holding(own_name, levels)
                levels = tuple(
                    level + self.tally_of(statement.callee, pair).delta
                    for pair, level in zip(PAIRED_TOOLS, levels)
                )
        return levels

    def _enter_holding(self, name: str, levels: tuple[int, ...]) -> None:
        """이 그래프의 `def` 하나를 그 카운터 값으로 걷는다. 같은 값이면 한 번만 걷는다."""
        key = (name, levels)
        if key in self._holding_walked:
            return
        self._holding_walked.add(key)
        self._walk_holding(self.statements_of(name), levels)

    def _reject_pause_while_holding(
        self,
        statement: MacroCheckpointStatement | MacroMatchStatement,
        levels: tuple[int, ...],
    ) -> None:
        """턴을 agent 에게 돌려줄 수 있는 자리에서 카운터가 0 이 아니면 거절한다.

        `match` 도 그런 자리다. Jev 가 `NONE_FITS` 를 고르면 `checkpoint` 와 같은 방식으로
        멈춘다.
        """
        for pair, level in zip(PAIRED_TOOLS, levels):
            if level == 0:
                continue
            if isinstance(statement, MacroMatchStatement):
                raise MacroRejection(
                    f"`{statement.source}` sits while {pair.counter} is still taken "
                    f"(`{pair.opens}` before it, `{pair.closes}` after). When Jev finds "
                    "no fitting case the macro pauses there, and the turn would end "
                    f"with the game in that state. Move the `match` before "
                    f"`{pair.opens}` or after `{pair.closes}`."
                )
            raise MacroRejection(
                f"`{statement.source}` sits while {pair.counter} is "
                f"still taken (`{pair.opens}` before it, `{pair.closes}` "
                "after). The turn would end with the game in that "
                f"state. Move the checkpoint before `{pair.opens}` or "
                f"after `{pair.closes}`."
            )

    def reject_unpaired(self) -> None:
        """누른 것을 안 푼 macro 를 거절한다.

        tool 을 직접 부르는 agent 는 그 tool 의 docstring("Nothing releases this for
        you.")을 다음 턴에 다시 읽는다. macro 는 글이 저작 시점에 고정이라 읽어 줄 다음
        턴이 없고, 눌린 채로 남은 키는 그 뒤의 모든 step 을 조용히 바꾼다. 반복이 없어
        반복 몸통은 한 회 안에서 0 이어야 하므로(`_tally_body`) 반복이 있어도 여기서 셀 수
        있다.
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
