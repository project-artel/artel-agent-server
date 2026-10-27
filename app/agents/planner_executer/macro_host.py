"""The `MacroHost` the executer runs the planner's macros with.

The macro runner (`app/agents/qa/macro/runner.py`) is used as it is: it reads a parsed macro and
asks its host to act, to read, to wait and to ask Jev. This host does what the tool loop's
`macro_tools._Host` does, over the run's channel:

| `MacroHost`      | here                                                                          |
|------------------|-------------------------------------------------------------------------------|
| `run`            | `QaRunChannel.act_and_look`, the call the tool loop's `ToolContext.act` makes   |
| `memories`       | the channel's `SceneMemory`                                                     |
| `wait_for_pulse` | `QaRunChannel.wait_for_reading`                                                 |
| `checkpoint`     | does not wait: the macro stops there and the turn ends                          |
| `decide`/`ask`   | `JevClient` from `app/qa/jev.py`, the client the tool loop's host uses          |

operator 의 말은 runner 에 넘기지 않는다. 이 engine 에는 답할 사람이 없고, 넘기면 macro 가
`OPERATOR_INTERRUPTED` 로 멈춘다. 받은 말은 비우고 turn 기록에 남긴다.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from app.agents.qa.macro.binding import MacroMemories
from app.agents.qa.macro.runner import (
    MacroAskRequest,
    MacroDecideRequest,
    MacroJevUnavailable,
    MacroPulseWait,
    MacroRunResult,
)
from app.agents.qa.tools.tool_context import _is_press, _press_landing
from app.qa.acting import ActionOutcome, ScreenChange
from app.qa.channel import QaRunChannel
from app.qa.envelope import JsonRpcAction
from app.qa.jev import JevClient, JevError

# `ask` 의 참·거짓이 뜻하는 것. tool loop 의 `macro_tools._Host.ask` 와 같은 글이다.
ASK_TRUE_MEANS = "The answer to the question is yes."
ASK_FALSE_MEANS = "The answer to the question is no."


@dataclass
class ChannelMacroHost:
    """`run_macro` 가 바깥에 대고 하는 일을 run 의 channel 과 Jev client 에 잇는다."""

    channel: QaRunChannel
    jev: JevClient
    step: int
    # 보낸 batch 마다 {summary, ok, error, reached} 와, 사람이 읽을 값 — 종류·씬·누른 대상의 화면 글자·키·
    # 입력한 글(`describe_actions`). Evaluator 의 재현 절차가 이것으로 쓰이고 검사된다(ARTEL-1267).
    sent: list[dict[str, Any]] = field(default_factory=list)
    # `checkpoint` 나 `none_fits` 로 멈춘 이유.
    paused: str | None = None
    operator_messages: list[str] = field(default_factory=list)

    async def run(self, actions: list[JsonRpcAction], summary: str, step: int) -> ActionOutcome:
        pulse = self.channel.scene.pulse
        record: dict[str, Any] = {
            "summary": summary, "ok": False, "error": None, "reached": None, "blocked": None,
            **describe_actions(actions, pulse),
        }
        self.sent.append(record)
        result, looked = await self.channel.act_and_look(actions, summary, step)
        self._drain_operator()
        if result is None:
            record["error"] = "no answer from the game"
            return ActionOutcome(text=f"{summary}: the game reported no result")
        methods = {action.id: action.method for action in actions}
        returned, failures = [], []
        for item in result.results:
            if item.id not in methods:
                continue
            if item.success:
                returned.append(item.returnValue)
            else:
                failures.append(f"{methods[item.id]}: {item.error or 'no reason given'}")
        record["ok"] = not failures
        record["error"] = "; ".join(failures) or None
        record["reached"] = next(
            (value["reached"] or "nothing" for value in returned if _is_press(value)), None
        )
        record["blocked"] = next(
            (value["blockedBy"] for value in returned if _is_press(value) and value.get("blockedBy")), None
        )
        # 겨눈 객체보다 실제로 닿은 객체의 글자가 재현에 맞다. 닿은 경로가 pulse 에 있으면 그것으로 바꾼다.
        landed = object_text(pulse, record["reached"]) if record["reached"] not in (None, "nothing") else None
        if landed and record["kind"] != "drag":
            record["target_text"] = landed
        if looked:
            screen = ScreenChange.moved
        elif self.channel.scene.pulse.seen:
            screen = ScreenChange.still
        else:
            screen = ScreenChange.unknown
        return ActionOutcome(
            text=f"{summary}: " + (f"FAILED — {record['error']}" if failures else "ok"),
            screen=screen,
            landings=tuple(_press_landing(value) for value in returned if _is_press(value)),
        )

    def memories(self) -> MacroMemories:
        return MacroMemories(scene=self.channel.scene)

    async def wait_for_pulse(self, timeout: float) -> MacroPulseWait:
        loop = asyncio.get_running_loop()
        started = loop.time()
        pulses = await self.channel.wait_for_reading(timeout)
        self._drain_operator()
        return MacroPulseWait(pulses, loop.time() - started)

    async def checkpoint(self, result: MacroRunResult) -> bool:
        if result.paused_decision is not None:
            self.paused = f"`decide` found no case that fits: {result.paused_reason}"
        else:
            self.paused = f"stopped at a checkpoint: {result.paused_reason}"
        return False

    async def decide(self, request: MacroDecideRequest) -> str:
        options = {option.name: option.description for option in request.options}
        try:
            return await self.jev.choose(request.question, options, request.state)
        except JevError as error:
            raise MacroJevUnavailable(str(error)) from error

    async def ask(self, request: MacroAskRequest) -> float:
        try:
            return await self.jev.probability(
                request.question, request.state, true_means=ASK_TRUE_MEANS, false_means=ASK_FALSE_MEANS
            )
        except JevError as error:
            raise MacroJevUnavailable(str(error)) from error

    def _drain_operator(self) -> None:
        self.operator_messages.extend(self.channel.drain_operator_messages())


# `move_mouse` 가 두 번이면 드래그, `mouse_down` 이 두 번이면 더블 클릭. 그 밖은 method 하나가 종류다.
_KIND_BY_METHOD = {
    "key_click": "key", "key_down": "key_down", "key_up": "key_up", "enter_text": "text",
    "mouse_down": "hold", "mouse_up": "release", "set_axis": "axis", "set_button": "button",
    "pause_time": "time", "resume_time": "time", "move_mouse": "move",
}


def describe_actions(actions: list[JsonRpcAction], pulse: Any) -> dict[str, Any]:
    """보낸 batch 를 사람이 읽을 값으로: 종류, 씬, 겨눈 대상과 그 화면 글자, 키, 입력한 글.

    Evaluator 의 재현 절차는 모델이 한글로 쓰되 여기 적힌 글자를 그대로 써야 하고, 코드가 그것을
    검사한다(ARTEL-1267). selector 경로는 사람이 따라 할 수 없어 `target` 에만 남기고 문장에는
    못 들어간다. 글자가 없는 객체(스프라이트)는 `target_text` 가 `None` 이고 `target_name` 이
    경로의 마지막 이름이다.
    """
    methods = [action.method for action in actions]
    if methods.count("move_mouse") >= 2 and "mouse_down" in methods:
        kind = "drag"
    elif methods.count("mouse_down") >= 2:
        kind = "double_click"
    elif "move_mouse" in methods and "mouse_down" in methods:
        kind = "click"
    else:
        kind = _KIND_BY_METHOD.get(methods[0], "other") if methods else "other"
    described: dict[str, Any] = {
        "kind": kind, "scene": getattr(pulse, "scene", None), "target": None, "target_name": None,
        "target_text": None, "to_text": None, "key": None, "value": None,
    }
    aims = [action.params[0] for action in actions if action.method == "move_mouse" and action.params]
    if aims:
        described["target"] = _ref(aims[0])
        described["target_name"] = object_name(aims[0])
        described["target_text"] = object_text(pulse, aims[0])
        if kind == "drag":
            described["to_text"] = object_text(pulse, aims[-1]) or object_name(aims[-1])
    for action in actions:
        if action.method in ("key_click", "key_down", "key_up") and action.params:
            described["key"] = str(action.params[0])
        elif action.method == "enter_text" and len(action.params) > 1:
            described["target"] = _ref(action.params[0])
            described["target_name"] = object_name(action.params[0])
            described["target_text"] = object_text(pulse, action.params[0])
            described["value"] = str(action.params[1])
    return described


def object_text(pulse: Any, ref: Any) -> str | None:
    """`ref`(selector·path·instance id)가 가리키는 객체가 지금 화면에 띄운 글자. 없으면 `None`."""
    held = getattr(pulse, "held", None) or {}
    for obj in held.values():
        if (isinstance(ref, str) and ref in (obj.selector, obj.path)) or (isinstance(ref, int) and ref == obj.id):
            text = (obj.text or "").strip()
            return text or None
    return None


def object_name(ref: Any) -> str | None:
    """경로의 마지막 이름에서 `(Clone)` 과 `[n]` 을 뗀 것. 글자가 없는 객체의 이름표다."""
    if not isinstance(ref, str) or not ref:
        return None
    last = ref.rsplit("/", 1)[-1]
    while last.endswith("]") and "[" in last:
        last = last[: last.rindex("[")]
    return last.replace("(Clone)", "").strip() or None


def _ref(ref: Any) -> str:
    return ref if isinstance(ref, str) else str(ref)
