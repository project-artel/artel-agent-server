"""Step-scoped game functions for the ORDERED planner and its active action."""

import json
import math

from app.agents.qa.tools.game_function_tools import CheatCall, allowed_for_step, invoke_cheat
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.game_functions import GameFunctionEntry, render_catalog, resolve_target

PARAM_TYPES = {"string": (str,), "enum": (str,), "int": (int,), "bool": (bool,), "float": (int, float)}


class PlannerGameFunctions:
    def __init__(self, ctx: ToolContext) -> None:
        self.ctx = ctx

    def available(self, step: int) -> list[GameFunctionEntry]:
        ctx = self.ctx
        state = ctx.state
        if ctx.arch.cheats != "on" or state.execution_mode != "ORDERED" or not 1 <= step <= len(state.step_meta):
            return []
        _, verifies = state.step_meta[step - 1]
        case = state.case_functions_at(step)
        explicit = case.cheats if case is not None and isinstance(case.cheats, list) else ()
        names = allowed_for_step(ctx, "cheat", step)
        return [
            entry for entry in ctx.channel.game_functions.cheats
            if entry.name in names and entry.name != "reset_game"
            and (entry.read_only or not verifies or entry.name in explicit)
        ]

    def guidance(self, step: int) -> str:
        entries = self.available(step)
        if not entries:
            return ""
        return (
            render_catalog(entries, None, unified=True)
            + "\nIn an action body only, call_game_function(name, arguments, target) invokes a listed function. "
            "arguments is a JSON object encoded as a string, default '{}'; omit target for static functions. "
            "For example, for a listed bool parameter enabled pass '{\"enabled\":true}'. "
            "Use setup only to establish the requested precondition. Perform the tested gameplay action normally. "
            "A setup response is never evidence that the tested action passed. Observe the actual outcome.\n\n"
        )

    async def call(self, step: int, name: str, arguments: str, target: str | None) -> CheatCall:
        entry = next((item for item in self.available(step) if item.name == name), None)
        if entry is None:
            return CheatCall.refused(f"Step {step} does not permit game function {name!r}. Nothing was sent.")
        live = [item for item in self.ctx.channel.scene.pulse.functions.values()
                if item.kind == "cheat" and item.name == name]
        if not entry.is_static and not target and len(live) > 1:
            return CheatCall.refused("Several instances provide this function. Specify the target. Nothing was sent.")
        if target and not entry.is_static:
            path, error = resolve_target(target, [item.target for item in live if item.target])
            if error:
                return CheatCall.refused(error)
            live = [item for item in live if item.target == path]
        if live and not any(item.available for item in live):
            return CheatCall.refused(f"{name} is currently unavailable. Nothing was sent.")
        try:
            values = json.loads(arguments)
            validate_arguments(entry, values)
        except (ValueError, TypeError, RecursionError) as error:
            return CheatCall.refused(f"Invalid arguments for {name}: {error}. Nothing was sent.")
        return await invoke_cheat(self.ctx, step, name, values, target)


def validate_arguments(entry: GameFunctionEntry, values: object) -> None:
    if not isinstance(values, dict):
        raise ValueError("expected a JSON object")
    unknown = values.keys() - {p.name for p in entry.params}
    if unknown:
        raise ValueError(f"unknown parameters {sorted(unknown)}")
    for param in entry.params:
        if param.name not in values:
            if param.default is not None:
                continue
            raise ValueError(f"missing {param.name}")
        value = values[param.name]
        if type(value) not in PARAM_TYPES.get(param.type, ()):
            raise ValueError(f"{param.name} expects {param.type}")
        if param.values and value not in param.values:
            raise ValueError(f"{param.name} expects one of {param.values}")
        if type(value) in (int, float):
            if (type(value) is float and not math.isfinite(value)) or (param.min is not None and value < param.min) or (
                param.max is not None and value > param.max
            ):
                raise ValueError(f"{param.name} is outside the registered range")
