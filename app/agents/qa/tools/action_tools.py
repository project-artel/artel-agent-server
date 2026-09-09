"""게임을 실제로 움직이는 도구 열여섯 개.

전부 `ctx.run` 을 지나간다. 거기서 `JsonRpcAction` 한 배치가 SDK 로 나가고, 그 행위
이후에 쌓인 `pulse` 만이 결과로 돌아온다.
"""

import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
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

    @tool
    async def click_button(step: int, target_id: int, thought: str) -> str:
        """DEPRECATED — use `click` instead.

        This invokes a Button's `onClick` directly: it never goes near the
        pointer or the EventSystem, so it shows no occlusion — a dialog sitting
        on top of the button still "clicks" it — and it reaches nothing but a
        `Button`. `click` goes through the pointer and the EventSystem instead,
        the same path a real click takes, so it exercises the game's own click
        path and reaches anything the scene shows, not only a `Button`.

        Kept, and its behaviour is unchanged: `target_id` must be an id from the
        scene you just saw. `step` is the scenario step this belongs to and
        `thought` is why you are clicking; both go on the timeline.
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
    async def move_pointer(step: int, target: str, thought: str) -> str:
        """Move the pointer to `target`, without pressing anything.

        `target` takes one of three forms — see `click` for the full
        explanation of all three, where the numbers come from, and what an id
        or a selector buys over a bare coordinate. Use this to hover, or to
        put the pointer somewhere before `hold_mouse_button` presses there.
        """
        try:
            params = parse_target(target)
        except ValueError as error:
            return str(error)
        return await _run(
            [JsonRpcAction(id=1, method="move_mouse", params=params)],
            f"Moving the pointer to {target}",
            step,
        )

    @tool
    async def click(step: int, target: str, thought: str, button: int = 0) -> str:
        """Click `target`. `button` is 0 for left, 1 for right, 2 for middle.

        `target` is one string, read as one of three forms, disambiguated by
        its shape:

        - a screen point: two numbers around one comma, e.g. "640,360" —
          screen pixels, taken from the scene view's `@ x,y` for an element
          exactly as printed. That value is the element's CENTRE and goes in
          verbatim, no conversion of any kind. Whitespace around the numbers
          is tolerated. This form is checked strictly — exactly two numbers
          around exactly one comma — so a Unity object name that happens to
          contain a comma still reads as a selector, the third form below.
        - a Unity instance id: "#" followed by an integer, e.g. "#12345" —
          the same id the scene view prints in brackets before an element's
          name.
        - anything else: a selector, a Unity hierarchy path, e.g.
          "Root[0]/Canvas[1]/Card(Clone)[3]".

        An id or a selector is resolved by the SDK at the moment this action
        actually runs, not when you read the scene. That is what either buys
        over a bare coordinate: a card that moved, or animated in, since your
        last look is still hit where it now is, and the click cannot be off by
        a rect that went stale between your observation and this call.

        A target matching none of the three forms is refused before anything
        reaches the game, and the refusal names all three so you can fix it.

        Prefer this over pressing and releasing yourself: the move, the press
        and the release go to the game as ONE batch, which the game runs
        strictly in order, so the click cannot be interrupted or left with the
        button down.

        `click_button` is DEPRECATED — use this instead; its own docstring
        says why.
        """
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

    @tool
    async def double_click(step: int, target: str, thought: str, button: int = 0) -> str:
        """Double-click `target`, for something that only a double-click does.

        `target` and `button` are as in `click`. Both presses ride ONE batch,
        which the game runs strictly in order, so nothing lands between them —
        two separate `click` calls are two turns apart and the game reads them
        as two single clicks.

        Use `click` twice when the game wants two clicks. This one is for the
        gesture a game treats as its own: opening an item, equipping from a list.
        """
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

    @tool
    async def hold_mouse_button(step: int, thought: str, button: int = 0) -> str:
        """Press a mouse button and keep it down, at wherever the pointer now is.

        `button` is 0 for left, 1 for right, 2 for middle. The press happens at
        the current pointer position — move there first with `move_pointer`.

        This is for input the game reads as HELD — charging, a long press,
        anything behind `Input.GetMouseButton`. For a plain click use `click`,
        and for a plain drag `drag`: both ride one batch and cannot be left
        half-done.

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
    async def drag(
        step: int,
        from_target: str,
        to_target: str,
        thought: str,
        button: int = 0,
    ) -> str:
        """Drag from `from_target` to `to_target` and drop there.

        Each end is independently one of the three forms `click` describes —
        a screen point, a Unity instance id, or a selector — so this reaches a
        drag `click` alone cannot: grab a card at a coordinate and drop it on a
        slot the scene gives an id for, or the reverse, or an id at both ends.
        Both ends are resolved by the SDK at the moment each move actually
        runs, which is why an id or a selector still lands correctly even if
        the thing it names has moved since you last looked. `button` is 0 for
        left, 1 for right, 2 for middle.

        Either end matching none of the three forms is refused before
        anything reaches the game — a drag that only half sends is worse than
        one that is refused outright.

        Prefer this over pressing and releasing yourself: the move, the press,
        the move and the release go to the game as ONE batch, which the game
        runs strictly in order, so the drag cannot be interrupted or left with
        the button down.
        """
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
