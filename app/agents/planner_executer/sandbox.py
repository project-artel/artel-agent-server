"""Loading a planner-written step module into a namespace the executer can call.

A step module's Python reads the game only through `g` and sends nothing: a func's `run` is a
macro, carried as a string. An `async def` other than `check` is refused with what to write
instead, so the planner rewrites it. This check rejects imports, private attribute
access and the builtins that reach outside (`open`, `eval`, `getattr` ...), and the module runs
with a short list of builtins. `global` and `nonlocal` are allowed: they reach only the module's
own namespace, and funcs use them to carry state between turns.

This is a guard against a planner's mistakes, not a security boundary. Python cannot be sandboxed
in-process against code written to escape; the module comes from our own planner, and the guard
exists so that one which wanders off — reading a file, importing `os` — fails loudly at load time
instead of doing it.
"""

import ast
import builtins
import linecache
from typing import Any

from app.agents.planner_executer import g as g_module

_FORBIDDEN_NAMES = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "breakpoint",
        "globals",
        "locals",
        "vars",
        "getattr",
        "setattr",
        "delattr",
        "__import__",
        "memoryview",
        "help",
        "exit",
        "quit",
    }
)

_ALLOWED_BUILTINS = (
    "abs all any bool dict enumerate filter float frozenset int isinstance len list "
    "map max min next print range repr reversed round set sorted str sum tuple zip "
    "Exception ValueError KeyError IndexError RuntimeError TypeError True False None"
).split()

_MACRO_RUN = (
    "a func's `run` is a macro, written as a string at top level: "
    "`run_<name> = '''def run_<name>() -> None: ...'''`, named in `FUNCS`. Inputs (`click`, "
    "`press_key`, ...), `decide` and `ask` are macro statements"
)

# Exceptions a step module may raise or catch by name.
_MODULE_NAMES: dict[str, Any] = {
    "WaitTimedOut": g_module.WaitTimedOut,
    "ExpectationFailed": g_module.ExpectationFailed,
}


class ModuleRejected(Exception):
    """The step module does not pass the load-time check."""


def check(source: str, coroutines: tuple[str, ...] = ()) -> ast.Module:
    """Parse and check `source`. Raises `ModuleRejected` naming the first problem.

    `coroutines` are the top-level `async def`s the module must define, each taking exactly `g`.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ModuleRejected(f"syntax error at line {error.lineno}: {error.msg}") from None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ModuleRejected(f"line {node.lineno}: imports are not allowed")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ModuleRejected(f"line {node.lineno}: private attribute `{node.attr}`")
        if isinstance(node, ast.Name) and (node.id in _FORBIDDEN_NAMES or node.id.startswith("__")):
            raise ModuleRejected(f"line {node.lineno}: `{node.id}` is not allowed")
        if isinstance(node, ast.AsyncFunctionDef) and node.name not in coroutines + ("check",):
            raise ModuleRejected(f"line {node.lineno}: `async def {node.name}` — {_MACRO_RUN}")

    _check_stands_alone(tree)
    defined = {node.name: node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)}
    for name in coroutines:
        if name not in defined:
            raise ModuleRejected(f"the module must define `async def {name}(g)` at top level")
        if len(defined[name].args.args) != 1:
            raise ModuleRejected(f"`{name}` takes exactly one argument, `g`")
    return tree


def _check_stands_alone(tree: ast.Module) -> None:
    """`check` 가 module 의 다른 top-level 함수를 쓰면 거절한다.

    `done`·`lost`·`when_*` 와 helper 는 play 를 설명한다. 판정이 그것을 빌려 쓰면 판정과 play 가 같은
    실수를 함께 한다 — `done` 이 틀린 값을 읽으면 play 는 거기서 멈추고 `check` 도 같은 값으로 통과한다.
    규칙은 하나다: `check` 는 `g` 로만 읽고, 읽기를 그 안에 적는다. `check` 안의 `lambda` 와 중첩
    `def` 는 된다.
    """
    top_level = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name != "check"
    }
    judge = next(
        (node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "check"),
        None,
    )
    if judge is None or not top_level:
        return
    bound = {
        node.id for node in ast.walk(judge) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    bound |= {
        node.name for node in ast.walk(judge)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not judge
    }
    bound |= {argument.arg for node in ast.walk(judge) if isinstance(node, ast.arguments) for argument in node.args}
    for node in ast.walk(judge):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in top_level
            and node.id not in bound
        ):
            raise ModuleRejected(
                f"line {node.lineno}: `check` uses `{node.id}`, a function this module defines. "
                "`check` must stand alone: it reads the game only through `g`, with every read "
                "written inside it (a `lambda` or a `def` nested in `check` is fine) — not through "
                "`done`, `lost`, a `when_` or a helper, which describe play"
            )


def load_namespace(
    source: str,
    coroutines: tuple[str, ...] = (),
    functions: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    filename: str = "<step-module>",
) -> dict[str, Any]:
    """Check `source`, run it, and return its namespace.

    Each named piece must be there — `check` (a coroutine), `done` (a function), `FUNCS` (a
    value) — so a module missing one is refused at load rather than failing halfway through a
    step. `filename` names the module in tracebacks; two namespaces loaded from one source (the
    funcs and the fixed `check`) need two names.
    """
    tree = check(source, coroutines)
    defined_functions = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assigned = {
        target.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        for target in n.targets
        if isinstance(target, ast.Name)
    }
    for name in functions:
        if name not in defined_functions:
            raise ModuleRejected(f"the module must define `def {name}(g)` at top level")
    for name in names:
        if name not in assigned:
            raise ModuleRejected(f"the module must assign `{name}` at top level")
    safe = {name: getattr(builtins, name) for name in _ALLOWED_BUILTINS if hasattr(builtins, name)}
    namespace: dict[str, Any] = {"__builtins__": safe, **_MODULE_NAMES}
    # Registered so a traceback through the module shows the line in the trace.
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    try:
        exec(compile(tree, filename, "exec"), namespace)  # noqa: S102 - checked above
    except Exception as error:  # noqa: BLE001 - a module that fails at top level is rejected
        raise ModuleRejected(f"the module raised while loading: {type(error).__name__}: {error}") from None
    return namespace
