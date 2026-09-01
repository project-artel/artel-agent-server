"""게임을 실제로 움직이는 도구 열여섯 개.

전부 `ctx.run` 을 지나간다. 거기서 `JsonRpcAction` 한 배치가 SDK 로 나가고, 그 행위
이후에 쌓인 `pulse` 만이 결과로 돌아온다.
"""

from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.qa.envelope import JsonRpcAction


def build_action_tools(ctx: ToolContext) -> list[BaseTool]:
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    _run = ctx.run

    @tool
    async def click_button(step: int, target_id: int, thought: str) -> str:
        """Click a button. `target_id` must be an id from the scene you just saw.

        `step` is the scenario step this belongs to and `thought` is why you are
        clicking; both go on the timeline.
        """
        return await _run(
            [JsonRpcAction(id=1, method="button_click", params=[target_id])],
            f"Clicking {target_id}",
            step,
        )

    @tool
    async def enter_text(step: int, target_id: int, value: str, thought: str) -> str:
        """Type into a text field. `target_id` must be an id from the current scene."""
        return await _run(
            [JsonRpcAction(id=1, method="enter_text", params=[target_id, value])],
            f"Typing into {target_id}",
            step,
        )

    @tool
    async def press_key(step: int, key_code: str, duration_seconds: float, thought: str) -> str:
        """Press a key — no target needed, so this works on a screen with nothing
        clickable, such as a dialogue or cutscene that advances on any key.

        `key_code` is a Unity KeyCode name, e.g. "Space", "Return", "Escape".
        `duration_seconds` must be greater than zero.
        """
        return await _run(
            [JsonRpcAction(id=1, method="key_click", params=[key_code, duration_seconds])],
            f"Pressing {key_code}",
            step,
        )

    @tool
    async def move_pointer(step: int, x: float, y: float, thought: str) -> str:
        """Move the pointer to a point on the screen, without pressing anything.

        `x` and `y` are screen pixels, taken from the scene exactly as printed:
        an element's `@ x,y` is its centre, and it belongs here unchanged — no
        conversion of any kind. Use this to hover, or to put the pointer
        somewhere a target id cannot address — a map, a canvas, an inventory slot.
        """
        return await _run(
            [JsonRpcAction(id=1, method="move_mouse", params=[x, y])],
            f"Moving the pointer to ({x}, {y})",
            step,
        )

    @tool
    async def click_at(step: int, x: float, y: float, thought: str, button: int = 0) -> str:
        """Click a point on the screen, for something the scene gives no id for.

        Coordinates are screen pixels, taken from the scene unchanged, as with
        `move_pointer`. `button` is 0 for left, 1 for right, 2 for middle.

        Prefer this over pressing and releasing yourself: the move, the press and
        the release go to the game as ONE batch, which the game runs strictly in
        order, so the click cannot be interrupted or left with the button down.

        `click_button` is the one to use when the scene DOES give an id — it
        presses what the game wired the button to, rather than a point that may
        be covered by something else.
        """
        return await _run(
            [
                # 누르기는 좌표를 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저 옮긴다 —
                # `drag_pointer` 가 같은 이유로 같은 순서를 쓴다.
                JsonRpcAction(id=1, method="move_mouse", params=[x, y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="mouse_up", params=[button]),
            ],
            f"Clicking at ({x}, {y})",
            step,
        )

    @tool
    async def double_click_at(
        step: int, x: float, y: float, thought: str, button: int = 0
    ) -> str:
        """Double-click a point, for something that only a double-click does.

        Coordinates and `button` are as in `click_at`. Both presses ride ONE
        batch, which the game runs strictly in order, so nothing lands between
        them — two separate `click_at` calls are two turns apart and the game
        reads them as two single clicks.

        Use `click_at` twice when the game wants two clicks. This one is for the
        gesture a game treats as its own: opening an item, equipping from a list.
        """
        return await _run(
            [
                # 누르기는 좌표를 안 받는다. 포인터가 있는 자리에 떨어지므로 먼저 옮긴다.
                JsonRpcAction(id=1, method="move_mouse", params=[x, y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="mouse_up", params=[button]),
                JsonRpcAction(id=4, method="mouse_down", params=[button]),
                JsonRpcAction(id=5, method="mouse_up", params=[button]),
            ],
            f"Double-clicking at ({x}, {y})",
            step,
        )

    @tool
    async def hold_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Press a mouse button and keep it down, at wherever the pointer now is.

        `button` is 0 for left, 1 for right, 2 for middle. The press happens at
        the current pointer position — move there first with `move_pointer`.

        This is for input the game reads as HELD — charging, a long press,
        anything behind `Input.GetMouseButton`. For a plain click use `click_at`,
        and for a plain drag `drag_pointer`: both ride one batch and cannot be
        left half-done.

        Nothing releases this for you. Call `release_mouse_button` before you
        judge the step, or every later step runs with the button still down.
        """
        return await _run(
            [JsonRpcAction(id=1, method="mouse_down", params=[button])],
            f"Holding mouse button {button}",
            step,
        )

    @tool
    async def release_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Let go of a mouse button held by `hold_mouse_button`.

        `button` must be the one you pressed: 0 for left, 1 for right, 2 for
        middle. The release lands at wherever the pointer now is, which is what
        decides where a drag drops.
        """
        return await _run(
            [JsonRpcAction(id=1, method="mouse_up", params=[button])],
            f"Releasing mouse button {button}",
            step,
        )

    @tool
    async def hold_key(step: int, key_code: str, thought: str) -> str:
        """Press a key and keep it down until you release it.

        `key_code` is a Unity KeyCode name, e.g. "W", "LeftShift", "Space". Use
        this for movement and modifiers — anything the game reads as "is it held
        right now" rather than "was it pressed". `press_key` is the one-shot.

        Nothing releases this for you. Call `release_key` before you judge the
        step, or the game keeps seeing the key down for the rest of the run.
        """
        return await _run(
            [JsonRpcAction(id=1, method="key_down", params=[key_code])],
            f"Holding {key_code}",
            step,
        )

    @tool
    async def set_input_axis(step: int, axis_name: str, value: float, thought: str) -> str:
        """Drive a named input axis, for a game that reads axes rather than keys.

        `axis_name` is a Unity Input Manager axis and is CASE SENSITIVE —
        "Horizontal", "Vertical", "Jump" are the stock ones. `value` runs from
        -1 to 1: 1 and -1 are the two directions, 0 is centred. A value outside
        that range is refused, and so is an axis the game has not set up, so a
        misspelled name comes back as an error rather than as silence.

        Use this when `hold_key` does nothing. A game that reads
        `Input.GetAxis("Horizontal")` cannot see a held key at all: the key tool
        reports success and the character does not move.

        Nothing centres this for you. Call it again with 0 before you judge the
        step, or every step after it runs with the axis pushed over.
        """
        return await _run(
            [JsonRpcAction(id=1, method="set_axis", params=[axis_name, value])],
            f"Setting axis {axis_name} to {value}",
            step,
        )

    @tool
    async def set_input_button(step: int, axis_name: str, pressed: bool, thought: str) -> str:
        """Hold or release a named input button, for a game that reads buttons by name.

        In Unity a button IS an axis: "Jump" is an axis entry whose positive side
        is a key, and the game may read it with `GetButton("Jump")` instead of
        checking the key itself. `axis_name` is that name, CASE SENSITIVE, and an
        axis the game has not set up comes back as an error.

        `pressed=True` holds it, `pressed=False` lets go. Release is what reports
        the button coming up, so a game watching for that edge needs the second
        call and not a value of 0.

        Nothing releases this for you. Call it with `pressed=False` before you
        judge the step, or the game keeps seeing the button down for the rest of
        the run.
        """
        return await _run(
            [JsonRpcAction(id=1, method="set_button", params=[axis_name, pressed])],
            f"{'Holding' if pressed else 'Releasing'} button {axis_name}",
            step,
        )

    @tool
    async def release_key(step: int, key_code: str, thought: str) -> str:
        """Let go of a key held by `hold_key`. `key_code` must be the same one."""
        return await _run(
            [JsonRpcAction(id=1, method="key_up", params=[key_code])],
            f"Releasing {key_code}",
            step,
        )

    @tool
    async def drag_pointer(
        step: int,
        from_x: float,
        from_y: float,
        to_x: float,
        to_y: float,
        thought: str,
        button: int = 0,
    ) -> str:
        """Drag from one point on the screen to another and drop there.

        Coordinates are screen pixels, taken from the scene unchanged, as with
        `move_pointer`. `button` is 0 for left, 1 for right, 2 for middle.

        Prefer this over pressing and releasing yourself: the press, the move and
        the release go to the game as ONE batch, which the game runs strictly in
        order, so the drag cannot be interrupted or left with the button down.
        """
        return await _run(
            [
                # The press takes no coordinates — it lands wherever the pointer
                # already is, so the drag has to start by moving there.
                JsonRpcAction(id=1, method="move_mouse", params=[from_x, from_y]),
                JsonRpcAction(id=2, method="mouse_down", params=[button]),
                JsonRpcAction(id=3, method="move_mouse", params=[to_x, to_y]),
                JsonRpcAction(id=4, method="mouse_up", params=[button]),
            ],
            f"Dragging from ({from_x}, {from_y}) to ({to_x}, {to_y})",
            step,
        )

    @tool
    async def pause_game_time(step: int, thought: str) -> str:
        """Freeze game time, so the screen stops changing while you read it.

        Use this when the thing you have to judge does not stay still — a hit
        effect, a countdown, a toast that disappears, a cutscene that plays past
        the moment the step is about. Clicking, typing and observing all keep
        working while time is frozen, because they do not run on game time.

        Nothing unfreezes this for you. Call `resume_game_time` before you report
        the step, or every step after it runs against a stopped game.
        """
        return await _run(
            [JsonRpcAction(id=1, method="pause_time")],
            "Pausing game time",
            step,
        )

    @tool
    async def resume_game_time(step: int, thought: str) -> str:
        """Let game time run again, at the speed it had before the pause.

        Fails if the game was not paused by `pause_game_time` — the speed a game
        chose for itself is not yours to overwrite.
        """
        return await _run(
            [JsonRpcAction(id=1, method="resume_time")],
            "Resuming game time",
            step,
        )

    @tool
    async def reset_game(step: int, thought: str, clear_player_prefs: bool = False) -> str:
        """Put the game back to the state the run started in.

        For a step that needs a clean game and no path back to one — a tutorial
        that plays once a session, a level already cleared, a wrong branch taken
        three screens ago. Cheaper than asking the operator to restart, and it
        keeps the run alive.

        It reloads the game's first scene, so everything on screen now is gone,
        and so is whatever the game was keeping across scene loads: managers,
        score, inventory. A `pause_game_time` freeze and any held key or mouse
        button are released first, so the fresh game starts with nothing pressed.
        Every target id you have is dead afterwards; observe before you act again.

        `clear_player_prefs=True` also deletes the game's PlayerPrefs — the small
        key/value store a game keeps its "tutorial seen" flag, its difficulty and
        volume settings, and its high score in. The SDK's own entries are kept,
        so the run itself survives. Ask for it only when the thing standing in
        your way outlives a restart: an intro or tutorial the game plays once per
        install rather than once per session, a setting saved by an earlier run,
        a high score the step is judging. A gate that lasts only the session is
        already gone after a plain reset, and the flag buys you nothing there. Do
        not ask for it when the step's precondition is *having* progress — the
        wipe deletes the very thing that step needs.

        The wipe is irreversible. There is no restore, and every later step and
        every later scenario in this run inherits the emptied store.

        Even with the flag on, the game's own save files are untouched. A game
        that writes its progress to a file of its own comes back holding it, so a
        step that depends on a fresh save file still needs the operator. An
        emptied store is also not a promise that the game is in a first-run
        state: a manager destroyed by the reload can write its keys straight back
        in `OnDestroy`.

        A game built on an SDK older than this flag ignores it and resets scene
        state only, and this tool cannot tell — the reset reports success either
        way. So when a step depended on the wipe and the game still behaves as
        though the data is there, report the step on what you actually saw
        instead of resetting again; the retry does the same thing.
        """
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
        click_at,
        double_click_at,
        hold_mouse_button,
        release_mouse_button,
        hold_key,
        release_key,
        set_input_axis,
        set_input_button,
        drag_pointer,
        pause_game_time,
        resume_game_time,
        reset_game,
    ]
