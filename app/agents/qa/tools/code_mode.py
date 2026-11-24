"""현재 QA run의 도구만 호출하는 작은 Python 문법 인터프리터."""

import ast
import json
from uuid import uuid4

from langchain_core.tools import BaseTool, tool
from pydantic import ValidationError

from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import QaCancelled, with_operator_messages

MAX_CALLS = 16
MAX_SOURCE = 12000


class CodeError(ValueError):
    pass


def _literal(node):
    try:
        value = ast.literal_eval(node)
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError, OverflowError) as error:
        raise CodeError(f"Line {node.lineno}: expected a JSON-compatible literal.") from error


def _validate(statements, catalog, in_loop=False):
    for statement in statements:
        match statement:
            case ast.Expr(value=call) | ast.Assign(targets=[ast.Name()], value=call):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    raise CodeError(f"Line {statement.lineno}: expected a tool call.")
                if call.func.id not in catalog or call.args:
                    raise CodeError(f"Line {statement.lineno}: use an available tool with named arguments.")
                names = [argument.arg for argument in call.keywords]
                if None in names or len(set(names)) != len(names):
                    raise CodeError(f"Line {statement.lineno}: expected unique named arguments.")
                for argument in call.keywords:
                    if argument.arg not in catalog[call.func.id].args:
                        raise CodeError(f"Line {statement.lineno}: unknown argument {argument.arg}.")
                    if not isinstance(argument.value, ast.Name):
                        _literal(argument.value)
            case ast.If(test=ast.Compare(left=ast.Constant(value=str()), ops=[ast.In() | ast.NotIn()],
                                         comparators=[ast.Name()])):
                _validate(statement.body, catalog, in_loop)
                _validate(statement.orelse, catalog, in_loop)
            case ast.For(target=ast.Name(id="_"), iter=ast.Call(func=ast.Name(id="range"),
                         args=[ast.Constant(value=count)], keywords=[]), orelse=[]) if not in_loop:
                if type(count) is not int or not 1 <= count <= MAX_CALLS:
                    raise CodeError(f"Line {statement.lineno}: range accepts 1 to {MAX_CALLS}.")
                _validate(statement.body, catalog, True)
            case ast.Break() if in_loop:
                pass
            case _:
                raise CodeError(f"Line {statement.lineno}: unsupported statement.")


def build_code_tool(tools: list[BaseTool], ctx: ToolContext) -> BaseTool:
    catalog = {entry.name: entry for entry in tools}
    ctx.state.code_tool_catalog = "\n".join(
        f"{entry.name}: {entry.description}\n"
        f"{json.dumps(entry.get_input_schema().model_json_schema(), ensure_ascii=False, separators=(',', ':'))}"
        for entry in tools
    )
    description = (
        "Execute QA tools for this step using Python syntax. Call tools with named arguments. "
        "step and thought default to the outer call. Assign results to names, use "
        "if 'text' in result (or not in), else, for _ in range(3), and break. "
        "Arguments may be literals or prior results. The last executed tool result is returned. "
        "At most 16 calls per snippet. Capture, compaction and completion return control immediately. "
        "The system prompt lists the available tools and arguments."
    )
    remaining = ctx.arch.tool_call_limit(ctx.state.total_steps)

    @tool(description=description)
    async def run_code(step: int, thought: str, source: str) -> str:
        nonlocal remaining
        variables = {}
        calls = 0
        result = ""
        stopped = False

        def lookup(name):
            if name not in variables:
                raise CodeError(f"No result assigned to {name}.")
            return variables[name]

        async def execute(statements):
            nonlocal calls, remaining, result, stopped
            for statement in statements:
                if stopped or ctx.state.finished:
                    return
                if isinstance(statement, ast.Break):
                    return True
                if isinstance(statement, ast.For):
                    for _ in range(statement.iter.args[0].value):
                        if await execute(statement.body) or stopped:
                            break
                    continue
                if isinstance(statement, ast.If):
                    test = statement.test
                    present = test.left.value in lookup(test.comparators[0].id)
                    branch = statement.body if present == isinstance(test.ops[0], ast.In) else statement.orelse
                    if await execute(branch):
                        return True
                    continue

                if ctx.channel.cancelled:
                    raise QaCancelled()
                messages = ctx.channel.drain_operator_messages()
                if messages:
                    result = with_operator_messages(result, messages)
                    stopped = True
                    return
                cycle = ctx.state.phase_cycle
                refusals = cycle.refusals if cycle else 0
                instructions = len(ctx.channel.operator_instructions)
                call = statement.value
                entry = catalog[call.func.id]
                arguments = {
                    argument.arg: lookup(argument.value.id) if isinstance(argument.value, ast.Name)
                    else _literal(argument.value) for argument in call.keywords
                }
                if "step" in entry.args:
                    if arguments.setdefault("step", step) != step:
                        raise CodeError(f"Line {statement.lineno}: step must match run_code.")
                if "thought" in entry.args:
                    arguments.setdefault("thought", thought)
                if calls >= MAX_CALLS or remaining <= 0:
                    raise CodeError("Tool call budget reached.")
                calls += 1
                remaining -= 1
                call_id = uuid4().hex
                await ctx.channel.tool_call(entry.name, call_id, arguments, step)
                try:
                    result = await entry.ainvoke(arguments)
                except QaCancelled:
                    await ctx.channel.tool_result(entry.name, call_id, "Cancelled.", step)
                    raise
                except ValidationError as error:
                    await ctx.channel.tool_result(entry.name, call_id, str(error)[:4000])
                    raise CodeError(f"Line {statement.lineno}: {error}") from error
                except Exception as error:
                    await ctx.channel.tool_result(entry.name, call_id, str(error)[:4000], step)
                    raise
                await ctx.channel.tool_result(entry.name, call_id, str(result)[:4000], step)
                if isinstance(statement, ast.Assign):
                    variables[statement.targets[0].id] = result
                stopped = (
                    entry.name in {"capture_screen", "compact_context", "ask_user"}
                    or arguments.get("capture_after") is True
                    or ctx.state.finished or ctx.state.paused_macro is not None
                    or (cycle is not None and cycle.refusals != refusals)
                    or len(ctx.channel.operator_instructions) != instructions
                )
                if stopped:
                    result = with_operator_messages(result, ctx.channel.drain_operator_messages())

        try:
            if not source.strip() or len(source) > MAX_SOURCE:
                raise CodeError(f"Provide 1 to {MAX_SOURCE} characters of code.")
            statements = ast.parse(source).body
            _validate(statements, catalog)
            await execute(statements)
        except (SyntaxError, CodeError, RecursionError) as error:
            return f"Stopped after {calls} tool call(s): {error}"
        return result

    return run_code
