"""게임을 실제로 움직이는 도구 열여섯 개.

전부 `ctx.run` 을 지나간다. 거기서 `JsonRpcAction` 한 배치가 SDK 로 나가고, 그 행위
이후에 쌓인 `pulse` 만이 결과로 돌아온다.
"""

import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_tool_description
from app.qa.envelope import JsonRpcAction

# `parse_target` 이 판별하는 두 엄격한 모양. 나머지 전부는 selector 로 읽는다.
_SCREEN_POINT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$")
_INSTANCE_ID_RE = re.compile(r"^#(-?\d+)$")

_TARGET_FORMS = (
    'a screen point ("640,360"), a Unity instance id ("#12345"), or a Unity '
    'hierarchy path selector ("Root[0]/Canvas[1]/Card(Clone)[3]")'
)


def parse_target(target: str) -> list:
    """`target` 문자열 하나를 `move_mouse` 가 받는 params 로 바꾼다.

    성공하면 정확히 하나만 돌려준다 — 점이면 숫자 둘, id 면 정수 하나, selector 면
    문자열 하나. 세 형태 중 어디에도 안 맞으면 `ValueError` 를 낸다. 그 메시지가 세
    형태를 이름으로 대므로, 부르는 tool 은 그대로 에이전트에게 돌려주면 된다 — 게임에는
    아무것도 나가지 않은 채로.

    쉼표 형태는 엄격하게 본다: 숫자 둘이 쉼표 하나를 사이에 두고 정확히 그것뿐이어야
    점으로 읽는다. 그래서 쉼표가 든 Unity 오브젝트 이름은 selector 로 남는다.
    """
    stripped = target.strip()
    if not stripped:
        raise ValueError(f"A target cannot be empty. A target is {_TARGET_FORMS}.")

    point = _SCREEN_POINT_RE.match(stripped)
    if point is not None:
        return [float(point.group(1)), float(point.group(2))]

    if stripped.startswith("#"):
        instance_id = _INSTANCE_ID_RE.match(stripped)
        if instance_id is None:
            raise ValueError(
                f"'{target}' starts with '#' but is not a valid id. A target is "
                f"{_TARGET_FORMS}."
            )
        return [int(instance_id.group(1))]

    return [stripped]


def build_action_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    _run = ctx.run

    @tool(description=load_tool_description("click_button").body)
    async def click_button(step: int, target_id: int, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="button_click", params=[target_id])],
            f"Clicking {target_id}",
            step,
        )

    @tool(description=load_tool_description("enter_text").body)
    async def enter_text(step: int, target_id: int, value: str, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="enter_text", params=[target_id, value])],
            f"Typing into {target_id}",
            step,
        )

    @tool(description=load_tool_description("press_key").body)
    async def press_key(step: int, key_code: str, duration_seconds: float, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="key_click", params=[key_code, duration_seconds])],
            f"Pressing {key_code}",
            step,
        )

    @tool(description=load_tool_description("move_pointer").body)
    async def move_pointer(step: int, target: str, thought: str) -> str:
        try:
            params = parse_target(target)
        except ValueError as error:
            return str(error)
        return await _run(
            [JsonRpcAction(id=1, method="move_mouse", params=params)],
            f"Moving the pointer to {target}",
            step,
        )

    @tool(description=load_tool_description("click").body)
    async def click(step: int, target: str, thought: str, button: int = 0) -> str:
        try:
            params = parse_target(target)
        except ValueError as error:
            return str(error)
        return await _run(
            [
                # 누르기는 target 을 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저
                # 옮긴다 — `drag` 가 같은 이유로 같은 순서를 쓴다.
                JsonRpcAction(id=1, method="move_mouse", params=params),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="mouse_up", params=[button]),
            ],
            f"Clicking {target}",
            step,
        )

    @tool(description=load_tool_description("double_click").body)
    async def double_click(step: int, target: str, thought: str, button: int = 0) -> str:
        try:
            params = parse_target(target)
        except ValueError as error:
            return str(error)
        return await _run(
            [
                # 누르기는 target 을 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저 옮긴다.
                JsonRpcAction(id=1, method="move_mouse", params=params),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="mouse_up", params=[button]),
                JsonRpcAction(id=4, method="mouse_down", params=[button]),
                JsonRpcAction(id=5, method="mouse_up", params=[button]),
            ],
            f"Double-clicking {target}",
            step,
        )

    @tool(description=load_tool_description("hold_mouse_button").body)
    async def hold_mouse_button(step: int, thought: str, button: int = 0) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="mouse_down", params=[button])],
            f"Holding mouse button {button}",
            step,
        )

    @tool(description=load_tool_description("release_mouse_button").body)
    async def release_mouse_button(step: int, thought: str, button: int = 0) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="mouse_up", params=[button])],
            f"Releasing mouse button {button}",
            step,
        )

    @tool(description=load_tool_description("hold_key").body)
    async def hold_key(step: int, key_code: str, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="key_down", params=[key_code])],
            f"Holding {key_code}",
            step,
        )

    @tool(description=load_tool_description("set_input_axis").body)
    async def set_input_axis(step: int, axis_name: str, value: float, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="set_axis", params=[axis_name, value])],
            f"Setting axis {axis_name} to {value}",
            step,
        )

    @tool(description=load_tool_description("set_input_button").body)
    async def set_input_button(step: int, axis_name: str, pressed: bool, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="set_button", params=[axis_name, pressed])],
            f"{'Holding' if pressed else 'Releasing'} button {axis_name}",
            step,
        )

    @tool(description=load_tool_description("release_key").body)
    async def release_key(step: int, key_code: str, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="key_up", params=[key_code])],
            f"Releasing {key_code}",
            step,
        )

    @tool(description=load_tool_description("drag").body)
    async def drag(
        step: int,
        from_target: str,
        to_target: str,
        thought: str,
        button: int = 0,
    ) -> str:
        try:
            from_params = parse_target(from_target)
            to_params = parse_target(to_target)
        except ValueError as error:
            return str(error)
        return await _run(
            [
                # 누르기는 target 을 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저
                # from_target 으로 옮긴다.
                JsonRpcAction(id=1, method="move_mouse", params=from_params),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="move_mouse", params=to_params),
                JsonRpcAction(id=4, method="mouse_up", params=[button]),
            ],
            f"Dragging from {from_target} to {to_target}",
            step,
        )

    @tool(description=load_tool_description("pause_game_time").body)
    async def pause_game_time(step: int, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="pause_time")],
            "Pausing game time",
            step,
        )

    @tool(description=load_tool_description("resume_game_time").body)
    async def resume_game_time(step: int, thought: str) -> str:
        return await _run(
            [JsonRpcAction(id=1, method="resume_time")],
            "Resuming game time",
            step,
        )

    @tool(description=load_tool_description("reset_game").body)
    async def reset_game(step: int, thought: str, clear_player_prefs: bool = False) -> str:
        # 플래그가 꺼져 있으면 params를 아예 비운다. 기본 호출의 wire를 지금과 byte 단위로
        # 같게 두어야 이 파라미터를 모르는 옛 SDK가 아무 변화도 보지 않는다.
        params: list[Any] = [{"clearPlayerPrefs": True}] if clear_player_prefs else []
        return await _run(
            [JsonRpcAction(id=1, method="reset_game", params=params)],
            "Resetting the game",
            step,
        )

    return [
        click_button,
        enter_text,
        press_key,
        move_pointer,
        click,
        double_click,
        hold_mouse_button,
        release_mouse_button,
        hold_key,
        release_key,
        set_input_axis,
        set_input_button,
        drag,
        pause_game_time,
        resume_game_time,
        reset_game,
    ]
