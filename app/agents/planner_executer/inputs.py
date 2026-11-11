"""Planner·editor 가 매크로를 쓸 때 읽는, 이 빌드가 읽는 입력 (ARTEL-1432).

Planner 는 런 중에 매크로를 직접 쓰지만 게임 코드를 보지 못한다. 그래서 `press_key` 로 누를 키를 고르다 게임이 읽지 않는 키를
누르거나, Input Manager 축으로만 읽는 게임(RedRunner 의 `GetButtonDown("Jump")`)에 키를 보내 아무 일도 일어나지 않는다. SDK 의
`list_inputs` 가 그 빌드가 읽는 키, Input Manager 이름, 지금 켜진 Input System action 의 키를 답하고, 이 모듈이 그것을 step
요청에 실을 글로 바꾼다. step·scene 계획과 editor 의 다시 쓰기가 모두 매크로를 쓰므로 셋 다 같은 절을 읽는다.

content map 은 쓰지 않는다. 걷어낼 기능이라 거기에 입력을 더하지 않는다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError

from app.qa.envelope import JsonRpcAction

if TYPE_CHECKING:
    from app.qa.channel import QaRunChannel

LIST_INPUTS = "list_inputs"
LIST_INPUTS_TIMEOUT_SECONDS = 5.0
# SDK 가 모르는 method 에 답하는 문구(`ActionExecutor`). 이 거절만 옛 SDK 로 본다.
UNSUPPORTED = "Unsupported method"
# 키·축·action 각각 이만큼까지 싣는다. 넘으면 몇 개가 남았는지 적는다.
MAX_ITEMS = 40


class ActionKeys(BaseModel):
    name: str
    keys: list[str] = Field(default_factory=list)


class BuildInputs(BaseModel):
    """SDK `list_inputs` 의 returnValue (`InputListDto`)."""

    keys: list[str] = Field(default_factory=list)
    otherKeys: bool = False
    axes: list[str] = Field(default_factory=list)
    actions: list[ActionKeys] = Field(default_factory=list)


class InputsAnswer(BaseModel):
    """`inputs` 가 None 이면 이번에는 모른다. `supported` 가 거짓이면 이 SDK 는 `list_inputs` 를 모른다."""

    inputs: BuildInputs | None = None
    supported: bool = True


async def fetch_inputs(channel: QaRunChannel) -> InputsAnswer:
    """SDK 에 `list_inputs` 를 한 번 묻는다.

    SDK 가 `Unsupported method` 로 거절하면 그 action 을 모르는 옛 SDK 다 — 부르는 쪽이 그 런에서 다시 묻지 않는다. 답이 오지
    않거나(timeout), 다른 이유로 실패하거나, 답의 모양이 다르면 이번만 모르는 것으로 둔다. 참고로 싣는 절이라 런을 멈추지 않는다.
    `facts.watch_facts` 와 같은 길이다.
    """
    result = await channel.dispatch_actions(
        [JsonRpcAction(id=1, method=LIST_INPUTS, params=[])],
        message="list the inputs this build reads",
        timeout=LIST_INPUTS_TIMEOUT_SECONDS,
    )
    item = result.results[0] if result is not None and result.results else None
    if item is None:
        return InputsAnswer()
    if not item.success:
        return InputsAnswer(supported=not (item.error or "").startswith(UNSUPPORTED))
    try:
        return InputsAnswer(inputs=BuildInputs.model_validate(getattr(item, "returnValue", None) or {}))
    except ValidationError:
        return InputsAnswer()


def inputs_text(inputs: BuildInputs | None) -> str | None:
    """step 요청에 실을 글. 실을 것이 없으면 None.

    빈 목록을 "읽지 않는다"로 쓰지 않는다. 위빙되지 않은 빌드는 비어 있고, 그것은 "모른다"이기 때문에 그 줄을 뺀다.
    """
    if inputs is None:
        return None
    lines: list[str] = []
    if inputs.keys:
        lines.append(f"- Keys the game reads with `Input.GetKey*` (send with `press_key`/`hold_key`): {_listed(inputs.keys)}")
    if inputs.otherKeys:
        lines.append("- The game also reads keys it picks at run time or any key, so a key that is not listed may still work.")
    if inputs.axes:
        lines.append(
            "- Input Manager axes and buttons the game reads. A key press never reaches these; send "
            f"`set_input_axis(\"<name>\", <value>)` or `set_input_button(\"<name>\", True)`: {_listed(inputs.axes)}"
        )
    actions = [action for action in inputs.actions if action.keys]
    if actions:
        lines.append("- Input System actions enabled right now, and the keys that trigger them (send with `press_key`):")
        shown = actions[:MAX_ITEMS]
        lines.extend(f"  - {action.name}: {_listed(action.keys)}" for action in shown)
        if len(actions) > len(shown):
            lines.append(f"  - … {len(actions) - len(shown)} more actions")
    if not lines:
        return None
    return "Inputs this build reads (from the SDK):\n" + "\n".join(lines)


def _listed(names: list[str]) -> str:
    shown = names[:MAX_ITEMS]
    more = f", … {len(names) - len(shown)} more" if len(names) > len(shown) else ""
    return ", ".join(shown) + more


class InputsReader:
    """런 하나 동안 `list_inputs` 를 묻는다. planner 와 editor 가 하나를 나눠 쓴다.

    매번 새로 묻는다 — Input System action 은 씬마다 바뀐다. SDK 가 한 번 거절하면 옛 SDK 이므로 그 런에서는 다시 묻지 않는다.
    """

    def __init__(self, channel: QaRunChannel) -> None:
        self._channel = channel
        self._supported = True

    async def read(self) -> str | None:
        """요청에 실을 글. 모르거나 옛 SDK 면 None."""
        if not self._supported:
            return None
        answer = await fetch_inputs(self._channel)
        self._supported = answer.supported
        return inputs_text(answer.inputs)
