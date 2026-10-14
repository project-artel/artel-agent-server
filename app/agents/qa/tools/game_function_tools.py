"""게임 코드가 등록한 cheat 와 behavior 를 부르는 도구 넷 (ARTEL-1054).

`run_cheat` 는 `invoke_cheat` 를, 나머지 셋은 같은 이름의 action 을 보낸다. 계약은 workspace 의
`.plan/general/2026-10-14-game-functions-wire-contract.md` 3절이다. 전부 `ctx.act` 를 지나가므로
결과에는 다른 action tool 처럼 `pulse` view 가 붙는다 — cheat 뒤에 화면을 다시 보라는 규칙이
여기서 지켜진다.

보내기 전에 거절하는 경우가 다섯이다. 상한, run catalog 에 없는 이름, 지금 test case 가 허용하지
않는 이름, static 인데 `target` 을 준 경우, `target` 을 못 찾은 경우. 거절은 아무것도 보내지 않고
tool 결과로만 돌아간다.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_tool_description
from app.qa.channel import QaRunChannel
from app.qa.envelope import ActionResultItem, JsonRpcAction
from app.qa.game_functions import (
    ActiveBehavior,
    FunctionKind,
    RunningBehavior,
    UsedCheat,
    allowed_names,
    render_behavior_ended,
    render_running,
    resolve_target,
)

# `start_behavior` 의 `maxSeconds` 기본값과 범위. SDK 가 같은 범위를 검사하지만(계약 3절), 여기서
# 먼저 보면 왕복 하나를 아낀다.
DEFAULT_BEHAVIOR_SECONDS = 30
MAX_BEHAVIOR_SECONDS = 300

def allowed_for_step(ctx: ToolContext, kind: FunctionKind, step: int | None) -> list[str]:
    """그 step 의 test case 가 쓰게 해 주는 이름. test case 가 없는 step 은 run catalog 전부다."""
    run_names = ctx.channel.game_functions.names(kind)
    case = ctx.state.case_functions_at(step)
    return allowed_names(run_names, case.permission(kind) if case is not None else None)


def focus(ctx: ToolContext, step: int | None) -> None:
    """`pulse` view 의 function 줄이 이 step 의 test case 기준으로 그려지게 한다.

    축이 꺼진 쪽은 `None` 으로 둔다. 그러면 그 쪽 줄이 없고, 둘 다 꺼진 run 의 view 는 이 기능이
    생기기 전과 같다.
    """
    pulse = ctx.channel.scene.pulse
    pulse.allowed_cheats = (
        allowed_for_step(ctx, "cheat", step) if ctx.arch.cheats == "on" else None
    )
    pulse.allowed_behaviors = (
        allowed_for_step(ctx, "behavior", step) if ctx.arch.behaviors == "on" else None
    )


def _refusal(ctx: ToolContext, kind: FunctionKind, name: str, step: int) -> str | None:
    """이 이름을 이 step 에서 부를 수 없는 이유, 부를 수 있으면 `None`."""
    word = kind
    run_names = ctx.channel.game_functions.names(kind)
    if not run_names:
        return f"No {word}s were given to this run, so `{name}` was not sent."
    if name not in run_names:
        return (
            f"`{name}` is not a {word} this run was given. Nothing was sent. "
            f"This run's {word}s: {', '.join(run_names)}."
        )
    allowed = allowed_for_step(ctx, kind, step)
    if name not in allowed:
        case_id = ctx.state.case_id_at(step)
        which = f"Test case {case_id}" if case_id is not None else f"Step {step}"
        allows = (
            f"It allows: {', '.join(allowed)}." if allowed else f"It allows no {word}s."
        )
        return f"{which} does not allow the {word} `{name}`. Nothing was sent. {allows}"
    return None


def _resolved_target(
    ctx: ToolContext, kind: FunctionKind, name: str, target: str | None
) -> tuple[str | None, str | None]:
    """`(target, None)` 이면 보낼 값이고 `(None, why)` 면 거절이다.

    `target` 을 안 주면 `null` 로 보낸다. instance 를 고르는 것은 SDK 다 — 하나뿐이면 그것이고,
    없거나 여럿이면 `no active <Component>` 나 `pass target as one of` 로 답한다.

    준 `target` 은 그 function 의 `PULSE.functions` entry 의 `target` 중에서 찾는다. 계약이 live
    instance 마다 entry 를 하나씩 싣는다.
    """
    entry = ctx.channel.game_functions.entry(kind, name)
    if target is None or not target.strip():
        return None, None
    if entry is not None and entry.is_static:
        return None, f"`{name}` is static and takes no target. Nothing was sent. Leave `target` out."
    candidates = [
        one.target
        for one in ctx.channel.scene.pulse.functions.values()
        if one.kind == kind and one.name == name and one.target
    ]
    return resolve_target(target, candidates)


def _failed(item: ActionResultItem) -> str:
    return f"FAILED — {item.error or 'no reason given'}"


def _value(item: ActionResultItem) -> dict:
    return item.returnValue if isinstance(item.returnValue, dict) else {}


def build_cheat_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    state, arch = ctx.state, ctx.arch

    @tool(description=load_tool_description("run_cheat").body)
    async def run_cheat(
        step: int,
        name: str,
        thought: str,
        arguments: dict[str, Any] = {},
        target: str | None = None,
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_run_cheat.md`.
        #
        # 인자 이름이 wire 의 `args` 가 아닌 `arguments` 인 이유: `@tool` 이 `args` 라는 인자를
        # `v__args` 로 바꿔 받아서 함수에 닿지 않는다. `run_macro` 도 같은 이름을 쓴다.
        focus(ctx, step)
        if state.cheats_attempted >= arch.max_cheats_per_run:
            return (
                f"You have used all {arch.max_cheats_per_run} cheat calls this run allows. "
                "Nothing was sent."
            )
        refusal = _refusal(ctx, "cheat", name, step)
        if refusal is not None:
            return refusal
        resolved, why = _resolved_target(ctx, "cheat", name, target)
        if why is not None:
            return why
        state.cheats_attempted += 1
        answered: list[ActionResultItem] = []

        def describe(item: ActionResultItem) -> str:
            answered.append(item)
            if not item.success:
                return _failed(item)
            message = _value(item).get("message")
            return f"ok — {message}" if message else "ok"

        outcome = await ctx.act(
            [
                JsonRpcAction(
                    id=1,
                    method="invoke_cheat",
                    params=[{"name": name, "args": dict(arguments), "target": resolved}],
                )
            ],
            f"Cheat {name}",
            step,
            describe=describe,
        )
        state.used_cheats.append(
            UsedCheat(
                name=name,
                args=dict(arguments),
                target=resolved,
                success=bool(answered) and answered[0].success,
            )
        )
        return outcome.text

    return [run_cheat]


def build_behavior_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state, arch = ctx.channel, ctx.state, ctx.arch

    def _out_of_calls() -> str | None:
        if state.behavior_calls_attempted >= arch.max_behavior_calls_per_run:
            return (
                f"You have used all {arch.max_behavior_calls_per_run} behavior calls this run "
                "allows. Nothing was sent."
            )
        return None

    @tool(description=load_tool_description("start_behavior").body)
    async def start_behavior(
        step: int,
        name: str,
        thought: str,
        arguments: dict[str, Any] = {},
        target: str | None = None,
        max_seconds: int = DEFAULT_BEHAVIOR_SECONDS,
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_start_behavior.md`.
        focus(ctx, step)
        spent = _out_of_calls()
        if spent is not None:
            return spent
        if not 1 <= max_seconds <= MAX_BEHAVIOR_SECONDS:
            return (
                f"`max_seconds` must be 1 to {MAX_BEHAVIOR_SECONDS}. Nothing was sent. "
                "Use `extend_behavior` to keep a behavior running longer."
            )
        refusal = _refusal(ctx, "behavior", name, step)
        if refusal is not None:
            return refusal
        resolved, why = _resolved_target(ctx, "behavior", name, target)
        if why is not None:
            return why
        state.behavior_calls_attempted += 1

        def describe(item: ActionResultItem) -> str:
            if not item.success:
                return _failed(item)
            value = _value(item)
            behavior_id = value.get("behaviorId")
            if not isinstance(behavior_id, int):
                return "ok — the game gave no behavior id"
            # 다음 `PULSE` 가 오기 전까지 쓸 임시 줄. 1초 안에 `report_step` 이 와도 이 behavior 가
            # `active_behaviors` 와 test case 경계의 stop 에서 빠지지 않는다. 다음 `PULSE` 가
            # 목록을 통째로 바꾼다.
            pulse = channel.scene.pulse
            pulse.end_behavior(behavior_id)
            pulse.behaviors.append(
                RunningBehavior(
                    behavior_id=behavior_id,
                    name=name,
                    target=resolved,
                    seconds=0.0,
                    max_seconds=float(max_seconds),
                    report=value.get("report"),
                )
            )
            report = f" — {value['report']}" if value.get("report") else ""
            return f"started as #{behavior_id}, up to {max_seconds}s{report}"

        outcome = await ctx.act(
            [
                JsonRpcAction(
                    id=1,
                    method="start_behavior",
                    params=[
                        {
                            "name": name,
                            "args": dict(arguments),
                            "target": resolved,
                            "maxSeconds": max_seconds,
                        }
                    ],
                )
            ],
            f"Starting behavior {name}",
            step,
            describe=describe,
        )
        return outcome.text

    @tool(description=load_tool_description("stop_behavior").body)
    async def stop_behavior(
        step: int,
        thought: str,
        behavior_id: int | None = None,
        all: bool = False,
    ) -> str:
        # What the agent reads is `qa_run/<version>/tool_stop_behavior.md`.
        # 허용 검사가 없다. 멈추는 것은 어느 test case 에서든 된다.
        focus(ctx, step)
        spent = _out_of_calls()
        if spent is not None:
            return spent
        # 둘 중 정확히 하나여야 한다.
        if all == (behavior_id is not None):
            return (
                "Pass either `behavior_id` or `all: true`, not both and not neither. "
                "Nothing was sent."
            )
        state.behavior_calls_attempted += 1
        params: dict[str, Any] = (
            {"all": True, "reason": "stopped"}
            if all
            else {"behaviorId": behavior_id, "reason": "stopped"}
        )
        return (
            await ctx.act(
                [JsonRpcAction(id=1, method="stop_behavior", params=[params])],
                "Stopping behaviors" if all else f"Stopping behavior #{behavior_id}",
                step,
                describe=lambda item: _stopped_line(channel, item),
            )
        ).text

    @tool(description=load_tool_description("extend_behavior").body)
    async def extend_behavior(step: int, behavior_id: int, seconds: int, thought: str) -> str:
        # What the agent reads is `qa_run/<version>/tool_extend_behavior.md`.
        focus(ctx, step)
        spent = _out_of_calls()
        if spent is not None:
            return spent
        if seconds < 1:
            return "`seconds` must be at least 1. Nothing was sent."
        state.behavior_calls_attempted += 1

        def describe(item: ActionResultItem) -> str:
            if not item.success:
                return _failed(item)
            limit = _value(item).get("maxSeconds")
            if isinstance(limit, (int, float)):
                for running in channel.scene.pulse.behaviors:
                    if running.behavior_id == behavior_id:
                        running.max_seconds = float(limit)
                return f"#{behavior_id} now runs up to {limit:g}s from its start"
            return "ok"

        return (
            await ctx.act(
                [
                    JsonRpcAction(
                        id=1,
                        method="extend_behavior",
                        params=[{"behaviorId": behavior_id, "seconds": seconds}],
                    )
                ],
                f"Extending behavior #{behavior_id}",
                step,
                describe=describe,
            )
        ).text

    return [start_behavior, stop_behavior, extend_behavior]


def _stopped_line(channel: QaRunChannel, item: ActionResultItem) -> str:
    """`stop_behavior` 의 답. 멈춘 id 를 도는 목록에서 바로 뺀다 — `BEHAVIOR_ENDED` 도 곧 온다."""
    if not item.success:
        return _failed(item)
    stopped = [one for one in _value(item).get("stopped") or [] if isinstance(one, int)]
    for behavior_id in stopped:
        channel.scene.pulse.end_behavior(behavior_id)
    if not stopped:
        return "ok — nothing was running"
    return "stopped " + ", ".join(f"#{one}" for one in stopped)


def active_behaviors(ctx: ToolContext) -> list[ActiveBehavior]:
    """`report_step` 의 `STATUS` 에 싣는, 지금 도는 behavior."""
    return [
        ActiveBehavior(
            behaviorId=one.behavior_id, name=one.name, target=one.target, seconds=one.seconds
        )
        for one in ctx.channel.scene.pulse.behaviors
    ]


def running_lines(ctx: ToolContext) -> str:
    """`report_step` 결과에 붙는, 판정 뒤에도 도는 behavior 목록."""
    running = ctx.channel.scene.pulse.behaviors
    if not running:
        return ""
    listed = "\n".join(f"  {render_running(one)}" for one in running)
    return (
        f"\n\nStill running:\n{listed}\nThey keep running into the next step. Stop one with "
        "`stop_behavior` if the next step should not have it."
    )


async def stop_not_allowed_before(ctx: ToolContext, next_step: int) -> str:
    """`next_step` 이 다른 test case 의 첫 step 이면, 그 case 가 허용하지 않는 behavior 를 멈춘다.

    `report_step` 이 판정을 적은 뒤에 부른다. 그 다음이 새 test case 의 첫 step 이기 때문이다.
    `reason: "not_allowed"` 로 보내므로 SDK 의 `BEHAVIOR_ENDED.reason` 도 그 값이다. 상한을
    안 센다 — agent 가 부른 것이 아니다.
    """
    if ctx.arch.behaviors != "on":
        return ""
    if not 1 < next_step <= ctx.state.total_steps:
        return ""
    case_id = ctx.state.case_id_at(next_step)
    if case_id is None or case_id == ctx.state.case_id_at(next_step - 1):
        return ""
    allowed = set(allowed_for_step(ctx, "behavior", next_step))
    lines: list[str] = []
    for running in list(ctx.channel.scene.pulse.behaviors):
        if running.name in allowed:
            continue
        result = await ctx.channel.dispatch_actions(
            [
                JsonRpcAction(
                    id=1,
                    method="stop_behavior",
                    params=[{"behaviorId": running.behavior_id, "reason": "not_allowed"}],
                )
            ],
            f"Stopping behavior #{running.behavior_id}: test case {case_id} does not allow it",
            next_step,
        )
        ctx.channel.scene.pulse.end_behavior(running.behavior_id)
        sent = "" if result is not None else " (the game did not answer)"
        lines.append(
            f"Stopped #{running.behavior_id} {running.name}: test case {case_id} "
            f"does not allow it{sent}."
        )
    return "".join(f"\n\n{line}" for line in lines)


def with_behavior_endings(one: BaseTool, ctx: ToolContext) -> BaseTool:
    """`BEHAVIOR_ENDED` 를 결과 끝에 붙이는 같은 tool. behaviors 축이 켜진 run 에만 감싼다.

    `ToolContext.answer` 에 두지 않는 이유: phase gate 의 거절이나 인자 거절처럼 `answer` 를 안
    지나는 결과가 있다. 거기서 빠지면 behavior 가 끝난 소식이 한 턴 늦게 간다. 이름도 설명도
    `args` 도 그대로라 모델이 받는 tool 선언이 안 움직인다(`_phase_gated` 와 같다).
    """
    inner = one.coroutine
    if inner is None:  # pragma: no cover - QA tool 은 전부 `async def` 다
        return one

    async def appended(*args, **kwargs):
        result = await inner(*args, **kwargs)
        endings = ctx.channel.drain_behavior_endings()
        if not endings or not isinstance(result, str):
            return result
        lines = "\n".join(render_behavior_ended(ended) for ended in endings)
        return f"{result}\n\n{lines}"

    return one.model_copy(update={"coroutine": appended})
