"""What the tools put on the wire, not just what they return to the model.

A real run showed the gap: every tool answered the agent correctly while the
timeline stayed blind. `observe_scene` logged nothing at all because it took no
`thought`, and every row landed with `step` null because no tool passed one.
These pin the frames themselves — category, step, and the actions inside them.
"""

import asyncio
import json

from app.agents.qa.arch import default_resolved_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.vision import MAX_CAPTURES_PER_RUN
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


def make(total_steps: int = 1, timeout: float = 0.05):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout, write_timeout=timeout)
    state = QaRunState(total_steps=total_steps)
    tools = {tool.name: tool for tool in build_tools(channel, state)}
    return channel, state, tools, sent


def scene(observables: dict) -> dict:
    return {
        "type": "GAME_STATE",
        "payload": {"scene": "Lobby", "interactables": [], "observables": observables},
    }


def actions(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.ACTION.value]


def logs(sent: list[dict]) -> list[dict]:
    return [frame for frame in sent if frame["type"] == MessageType.LOG.value]


def answer(
    channel: QaRunChannel,
    sent: list[dict],
    results: list[dict] | None = None,
    observables: dict | None = None,
):
    """Reply the way the game does: the scene, then the batch's ACTION_RESULT.

    `observables=None` means the game stayed silent about the scene. The result
    has to quote the ACTION's messageId, so this waits for the frame to go out
    rather than assuming it is already there when the task first runs.
    """
    already = len(actions(sent))

    async def reply() -> None:
        for _ in range(50):
            if len(actions(sent)) > already:
                break
            await asyncio.sleep(0)
        if observables is not None:
            channel.on_game_state(scene(observables))
        channel.on_action_result(
            {
                "correlationId": actions(sent)[-1]["messageId"],
                "payload": {"results": results or []},
            }
        )

    return asyncio.create_task(reply())


def test_observe_returns_the_change_since_the_last_look() -> None:
    """프레임을 게임이 스스로 올린다 — `PollSceneState` 가 하는 일이다.

    묻고 답하는 대신 밀어 넣는 모양이 된 것이 ARTEL-516 이다. 워터마크가 그대로라
    "지난번 이후 무엇이 움직였나" 는 종전과 같이 나온다.
    """

    async def run() -> None:
        channel, _, tools, _ = make()

        channel.on_game_state(scene({"Score": {"value": 0}}))
        await tools["observe_scene"].ainvoke({"step": 1, "thought": "화면을 본다"})

        channel.on_game_state(scene({"Score": {"value": 100}}))
        second = await tools["observe_scene"].ainvoke(
            {"step": 1, "thought": "점수가 올랐는지 본다"}
        )

        # The second look reports the move, not just the current number.
        assert "Score: 0 → 100" in second

    asyncio.run(run())


def test_observe_says_so_when_the_game_is_silent() -> None:
    """Silence must come back as a value the agent can act on, not an exception."""

    async def run() -> None:
        _, _, tools, _ = make()
        result = await tools["observe_scene"].ainvoke({"step": 1, "thought": "화면을 본다"})
        assert "did not answer" in result

    asyncio.run(run())


def test_observing_asks_the_game_for_nothing() -> None:
    """`observe_scene` 이 프레임을 하나도 안 보낸다.

    종전에는 `scan_scene` 을 실은 ACTION 이 나갔다. 그 액션의 유일한 일이 `GAME_STATE` 를
    만드는 것인데, ARTEL-513 이 그 채널을 끄면 오류를 답하고 켜 두면 `PollSceneState` 가
    이미 같은 것을 스스로 올린다 — 어느 쪽에서도 하는 일이 없다(ARTEL-516).
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_pulse(reading())

        await tools["observe_scene"].ainvoke({"step": 2, "thought": "화면을 본다"})

        # 생각 한 줄조차 남지 않는다. 그 이유는 러너가 내는 TOOL 프레임의 인자로
        # 실린다(ARTEL-609).
        assert sent == []

    asyncio.run(run())


def test_a_tool_writes_no_thought_row_of_its_own() -> None:
    """`thought` 는 이제 tool 이 남기지 않는다(ARTEL-609).

    각 tool 이 제 이유를 LOG 한 줄로 적던 것이 로그를 산문으로 채운 원인이었다 — 무슨
    tool 이 불렸는지는 어디에도 없고 "씬 캡처 했습니다" 같은 문장만 남았다. 지금은 러너가
    호출마다 TOOL 프레임을 내고 `thought` 는 그 인자로 실린다. 그쪽 계약은
    `tests/test_qa_reasoning_log.py` 가 고정한다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, observables={})
        await tools["observe_scene"].ainvoke(
            {"step": 3, "thought": "로딩이 끝났는지 확인한다"}
        )

        assert logs(sent) == []

    asyncio.run(run())


def test_operator_message_is_appended_to_the_next_tool_result() -> None:
    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_chat({"payload": {"message": "메뉴로 가"}})

        answer(channel, sent, observables={})
        result = await tools["observe_scene"].ainvoke({"step": 1, "thought": "화면을 본다"})

        assert "메뉴로 가" in result
        # Delivered once; it must not repeat on every later call.
        answer(channel, sent, observables={})
        assert "메뉴로 가" not in await tools["observe_scene"].ainvoke(
            {"step": 1, "thought": "다시 본다"}
        )

    asyncio.run(run())


def test_pressing_a_key_needs_no_target_and_batches_no_tail() -> None:
    """press_key needs no target, so it works on a screen with nothing clickable."""

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["press_key"].ainvoke(
            {"step": 1, "key_code": "Space", "duration_seconds": 0.5, "thought": "대사를 넘긴다"}
        )

        methods = [a["method"] for a in actions(sent)[0]["payload"]["actions"]]
        # 꼬리가 사라진 자리. 액션 뒤의 화면은 판독이 실어 온다.
        assert methods == ["key_click"], "꼬리가 붙지 않는다(ARTEL-516)"

    asyncio.run(run())


def test_moving_the_pointer_sends_the_screen_coordinates() -> None:
    """The pointer tools address pixels, not ids — the params are the whole target."""

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["move_pointer"].ainvoke(
            {"step": 1, "x": 860, "y": 540, "thought": "칸 위로 옮긴다"}
        )

        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "move_mouse", "params": [860, 540]},
        ]

    asyncio.run(run())


def test_holding_and_releasing_a_mouse_button_carry_the_button_index() -> None:
    """Left by default, and the release has to name the same button as the press."""

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["hold_mouse_button"].ainvoke({"step": 1, "thought": "누른 채로 둔다"})
        await tools["release_mouse_button"].ainvoke(
            {"step": 1, "button": 1, "thought": "우클릭을 놓는다"}
        )

        emitted = [frame["payload"]["actions"][0] for frame in actions(sent)]
        assert [(item["method"], item["params"]) for item in emitted] == [
            ("mouse_down", [0]),
            ("mouse_up", [1]),
        ]

    asyncio.run(run())


def test_holding_and_releasing_a_key_carry_the_key_code() -> None:
    async def run() -> None:
        _, _, tools, sent = make()
        await tools["hold_key"].ainvoke(
            {"step": 1, "key_code": "W", "thought": "앞으로 계속 걷는다"}
        )
        await tools["release_key"].ainvoke(
            {"step": 1, "key_code": "W", "thought": "걷기를 멈춘다"}
        )

        emitted = [frame["payload"]["actions"][0] for frame in actions(sent)]
        assert [(item["method"], item["params"]) for item in emitted] == [
            ("key_down", ["W"]),
            ("key_up", ["W"]),
        ]

    asyncio.run(run())


def test_the_axis_tools_carry_the_axis_name_and_what_to_do_with_it() -> None:
    """Order matters in the params: the SDK reads them positionally.

    Both tools name the axis first, and the second slot is what differs — a
    number for the axis, a flag for the button. Swapped, the SDK refuses the call
    rather than acting on the wrong thing, but the run has still lost the step.
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["set_input_axis"].ainvoke(
            {"step": 1, "axis_name": "Horizontal", "value": 1, "thought": "오른쪽으로 민다"}
        )
        await tools["set_input_axis"].ainvoke(
            {"step": 1, "axis_name": "Horizontal", "value": 0, "thought": "축을 가운데로 되돌린다"}
        )
        await tools["set_input_button"].ainvoke(
            {"step": 1, "axis_name": "Jump", "pressed": True, "thought": "점프를 누른다"}
        )
        await tools["set_input_button"].ainvoke(
            {"step": 1, "axis_name": "Jump", "pressed": False, "thought": "점프를 놓는다"}
        )

        emitted = [frame["payload"]["actions"][0] for frame in actions(sent)]
        assert [(item["method"], item["params"]) for item in emitted] == [
            ("set_axis", ["Horizontal", 1]),
            ("set_axis", ["Horizontal", 0]),
            ("set_button", ["Jump", True]),
            ("set_button", ["Jump", False]),
        ]

    asyncio.run(run())


def test_every_tool_that_leaves_input_set_names_its_own_way_out() -> None:
    """Nothing prompts the undo except the description saying it exists.

    The key and mouse pairs have a partner tool, so the name alone hints at it.
    The axis tools do not — you undo `set_input_axis` by calling it again with 0
    — so if the description does not say so, the way back is written nowhere the
    model will read, and every later step runs with the axis pushed over.
    """
    _, _, tools, _ = make()

    # Descriptions are wrapped prose, so a phrase spanning a line break is one
    # the raw string does not contain. Normalise rather than reflow the docstring
    # to suit the assertion.
    described = {
        name: " ".join(tool.description.split()) for name, tool in tools.items()
    }

    assert "again with 0" in described["set_input_axis"]
    assert "pressed=False" in described["set_input_button"]
    for name in ("set_input_axis", "set_input_button"):
        assert "before you judge the step" in described[name], (
            f"{name} does not say when to undo what it set"
        )


def test_a_drag_goes_out_as_one_batch_in_order() -> None:
    """The order is the drag. Split across calls, anything could land between them.

    The press takes no coordinates, so the batch has to start by moving to where
    the drag begins — otherwise the button goes down wherever the pointer was
    left. The scan rides last, as it does for every acting tool.
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["drag_pointer"].ainvoke(
            {
                "step": 1,
                "from_x": 100,
                "from_y": 200,
                "to_x": 700,
                "to_y": 200,
                "thought": "카드를 슬롯으로 끌어다 놓는다",
            }
        )

        assert len(actions(sent)) == 1
        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "move_mouse", "params": [100, 200]},
            {"id": 2, "jsonrpc": "2.0", "method": "mouse_down", "params": [0]},
            {"id": 3, "jsonrpc": "2.0", "method": "move_mouse", "params": [700, 200]},
            {"id": 4, "jsonrpc": "2.0", "method": "mouse_up", "params": [0]},
        ]

    asyncio.run(run())


def test_a_click_goes_out_as_one_batch_in_order() -> None:
    """세 턴으로 쪼개면 그 사이에 게임이 돈다 — 누른 채로 다른 판단이 끼어들고, 실패하면
    눌린 채로 남는다.

    누르기는 좌표를 안 받으므로 배치가 옮기는 것으로 시작해야 한다. 안 그러면 포인터가
    마지막으로 있던 자리에서 눌린다.
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["click_at"].ainvoke(
            {"step": 1, "x": 409, "y": 500, "thought": "조합 칸을 누른다"}
        )

        assert len(actions(sent)) == 1
        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "move_mouse", "params": [409, 500]},
            {"id": 2, "jsonrpc": "2.0", "method": "mouse_down", "params": [0]},
            {"id": 3, "jsonrpc": "2.0", "method": "mouse_up", "params": [0]},
        ]

    asyncio.run(run())


def test_a_click_takes_the_button_it_is_given() -> None:
    """오른쪽·가운데 버튼도 같은 배치로 나간다. 누른 것과 뗀 것이 다르면 눌린 채로 남는다."""

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["click_at"].ainvoke(
            {"step": 1, "x": 10, "y": 20, "thought": "오른쪽 클릭", "button": 1}
        )

        methods = [(a["method"], a["params"]) for a in actions(sent)[0]["payload"]["actions"]]
        assert methods == [
            ("move_mouse", [10, 20]),
            ("mouse_down", [1]),
            ("mouse_up", [1]),
        ]

    asyncio.run(run())


def test_a_double_click_rides_one_batch() -> None:
    """`click_at` 두 번은 두 턴이고 실측으로 한 턴이 4초쯤이다. 게임이 더블클릭으로 치는
    간격은 0.3~0.5초라 절대 못 들어가고, 싱글클릭 두 번이 된다(ARTEL-675).

    그리고 실패가 조용하다 — 액션은 둘 다 ok 로 답하므로 화면이 안 바뀐 것만 남고, 그것은
    게임 결함과 구분이 안 된다.
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["double_click_at"].ainvoke(
            {"step": 1, "x": 300, "y": 400, "thought": "아이템을 더블클릭해 장착한다"}
        )

        assert len(actions(sent)) == 1
        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "move_mouse", "params": [300, 400]},
            {"id": 2, "jsonrpc": "2.0", "method": "mouse_down", "params": [0]},
            {"id": 3, "jsonrpc": "2.0", "method": "mouse_up", "params": [0]},
            {"id": 4, "jsonrpc": "2.0", "method": "mouse_down", "params": [0]},
            {"id": 5, "jsonrpc": "2.0", "method": "mouse_up", "params": [0]},
        ]

    asyncio.run(run())


def test_a_double_click_takes_the_button_it_is_given() -> None:
    async def run() -> None:
        _, _, tools, sent = make()
        await tools["double_click_at"].ainvoke(
            {"step": 1, "x": 1, "y": 2, "thought": "오른쪽 더블클릭", "button": 1}
        )

        buttons = {
            tuple(a["params"])
            for a in actions(sent)[0]["payload"]["actions"]
            if a["method"] != "move_mouse"
        }
        assert buttons == {(1,)}, "누른 버튼과 뗀 버튼이 다르면 눌린 채로 남는다"

    asyncio.run(run())


def test_pausing_and_resuming_go_out_as_the_time_actions() -> None:
    """멈추는 것도 다른 액션과 같은 액션이다. 배치에 실려 나가는 것은 그것 하나다.

    종전에는 꼬리 `scan_scene` 이 함께 탔다 — 따로 물으면 그 왕복 동안 멈춰서 읽으려던
    것이 이미 사라질 수 있었기 때문이다. 지금은 판독이 그 자리를 대신하고, 그래서 그
    왕복을 기다리는 대신 다음 배치를 기다린다(ARTEL-516).
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["pause_game_time"].ainvoke(
            {"step": 1, "thought": "이펙트가 사라지기 전에 멈춘다"}
        )
        await tools["resume_game_time"].ainvoke({"step": 1, "thought": "다시 진행시킨다"})

        assert [frame["payload"]["message"] for frame in actions(sent)] == [
            "Pausing game time",
            "Resuming game time",
        ]
        assert [
            [item["method"] for item in frame["payload"]["actions"]]
            for frame in actions(sent)
        ] == [["pause_time"], ["resume_time"]]

    asyncio.run(run())


def test_resetting_goes_out_as_the_reset_action() -> None:
    """리셋만 나간다. 새 화면은 판독이 실어 온다.

    에이전트가 쥔 target id 는 옛 씬과 함께 죽으므로 리셋 뒤에는 반드시 새 화면이 와야
    한다. 종전에는 꼬리 `scan_scene` 이 그것을 보장했고, 지금은 씬이 바뀌면 SDK 가 전량
    판독을 내는 것이 보장한다(`LiveState.Compose` 의 `everything`).
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["reset_game"].ainvoke(
            {"step": 1, "thought": "튜토리얼을 처음부터 다시 본다"}
        )

        assert [frame["payload"]["message"] for frame in actions(sent)] == [
            "Resetting the game"
        ]
        assert [item["method"] for item in actions(sent)[0]["payload"]["actions"]] == [
            "reset_game"
        ]
        # 기본 호출은 params가 빈 채로 나간다. clearPlayerPrefs 를 모르는 옛 SDK 가 보는
        # frame 이 지금과 동일해야 하므로, 이 단언이 그 하위 호환을 고정한다.
        assert actions(sent)[0]["payload"]["actions"][0]["params"] == []

    asyncio.run(run())


def test_a_reset_can_ask_for_the_player_prefs_to_go() -> None:
    """저장 데이터까지 지우라는 요청은 wire 파라미터 하나로 나간다.

    SDK 는 camelCase 로 읽으므로(`capture_screen` 의 `maxEdge`·`padding` 과 같은 규칙)
    Python 쪽 snake_case 인자가 `clearPlayerPrefs` 로 번역돼야 한다. frame 전체를 고정해
    키 이름이 조용히 어긋나는 것을 막는다.
    """

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["reset_game"].ainvoke(
            {
                "step": 1,
                "thought": "튜토리얼을 처음 보는 상태로 되돌린다",
                "clear_player_prefs": True,
            }
        )

        assert actions(sent)[0]["payload"]["actions"] == [
            {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "reset_game",
                "params": [{"clearPlayerPrefs": True}],
            }
        ]

    asyncio.run(run())


def test_the_reset_tool_says_what_the_wipe_does_not_reach() -> None:
    """지우는 것과 지우지 못하는 것이 둘 다 설명에 있어야 한다.

    `PlayerPrefs` 만 적혀 있으면 에이전트는 저장 데이터가 전부 사라진다고 읽는다. 플래그를
    켜도 게임 자신의 save file 은 그대로 남고, 그 경우엔 operator 가 필요하다는 기존
    탈출구가 여전히 유효하다.

    한계 문장은 통째로 고정한다. `"disk"` 나 `"save file"` 같은 조각은 플래그 이전 설명에도
    이미 있었으므로 아무것도 구별하지 못한다 — "wipe 가 save file 까지 지운다"로 뒤집어
    써도 그 조각들은 그대로 남아 테스트가 통과한다. 문장을 고쳐 쓰면 이 테스트가 깨지는데,
    그건 오타가 아니라 결정이다. 깨졌을 때 할 일은 단언을 느슨하게 푸는 것이 아니라 새 문장이
    같은 한계를 말하는지 확인하는 것이다.
    """
    _, _, tools, _ = make()

    description = tools["reset_game"].description

    assert "PlayerPrefs" in description
    assert "Even with the flag on, the game's own save files are untouched." in description
    assert "still needs the operator" in description


def test_resume_reports_the_games_refusal() -> None:
    """Resuming what nobody paused fails in the SDK, and the agent has to see it."""

    async def run() -> None:
        channel, _, tools, sent = make()

        task = answer(
            channel,
            sent,
            results=[
                {
                    "id": 1,
                    "success": False,
                    "error": "resume_time: game time was not paused by pause_time.",
                }
            ],
        )
        result = await tools["resume_game_time"].ainvoke(
            {"step": 1, "thought": "멈춰 있었는지 확인한다"}
        )
        await task

        assert "resume_time: FAILED — resume_time: game time was not paused" in result

    asyncio.run(run())


def test_waiting_for_the_operator_returns_what_they_said() -> None:
    async def run() -> None:
        channel, _, tools, sent = make()

        async def speak() -> None:
            await asyncio.sleep(0)
            channel.on_chat({"payload": {"message": "그 화면은 건너뛰어"}})

        asyncio.create_task(speak())
        result = await tools["wait_for_operator"].ainvoke(
            {"step": 2, "thought": "시나리오에 없는 화면이라 물어본다"}
        )

        assert "그 화면은 건너뛰어" in result
        # Nothing was asked of the game while it waited.
        assert actions(sent) == []

    asyncio.run(run())


def test_waiting_says_so_when_nobody_answers() -> None:
    """Silence comes back as a value, with the wait it actually made named."""

    async def run() -> None:
        _, _, tools, _ = make()
        result = await tools["wait_for_operator"].ainvoke(
            {"thought": "답을 기다린다", "timeout_seconds": 0.05}
        )

        assert "said nothing within 0.05s" in result

    asyncio.run(run())


def test_a_batch_result_names_which_action_failed() -> None:
    """Four outcomes in a row are unreadable unless each says what it belongs to.

    The trailing scan_scene is still ours, so its outcome must not be reported as
    something the agent asked for — with a batch that is id 5, not id 2.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        task = answer(
            channel,
            sent,
            results=[
                {"id": 1, "success": True},
                {"id": 2, "success": True},
                {"id": 3, "success": True},
                {"id": 4, "success": False, "error": "No drop target under the pointer"},
                {"id": 5, "success": True},
            ],
        )
        result = await tools["drag_pointer"].ainvoke(
            {
                "step": 1,
                "from_x": 100,
                "from_y": 200,
                "to_x": 700,
                "to_y": 200,
                "thought": "카드를 슬롯으로 끌어다 놓는다",
            }
        )
        await task

        assert "mouse_up: FAILED — No drop target under the pointer" in result
        assert result.count("move_mouse: ok") == 2
        assert "scan_scene" not in result

    asyncio.run(run())


def test_click_reports_the_failure_reason() -> None:
    async def run() -> None:
        channel, _, tools, sent = make()

        task = answer(
            channel,
            sent,
            results=[{"id": 1, "success": False, "error": "Unknown target id: 999"}],
        )
        result = await tools["click_button"].ainvoke(
            {"step": 1, "target_id": 999, "thought": "시작 버튼"}
        )
        await task

        assert "FAILED" in result
        assert "Unknown target id: 999" in result

    asyncio.run(run())


def test_every_outbound_frame_carries_the_step_number() -> None:
    """`step` was null on all 451 rows of a real run because no tool passed one.

    Without it the timeline cannot say which scenario step a row belongs to, so
    the ACTION, the STATUS and the CHAT all have to carry the number the agent
    named. LOG 는 이 목록에서 빠졌다 — tool 이 제 이유를 LOG 로 남기지 않게 되면서
    (ARTEL-609) 여기서 나오는 LOG 가 없다. 그 자리의 step 은 러너가 내는 TOOL 프레임이
    이고, `tests/test_qa_reasoning_log.py` 가 고정한다.
    """

    async def run() -> None:
        channel, _, tools, sent = make(total_steps=2)

        task = answer(channel, sent, results=[{"id": 1, "success": True}])
        await tools["click_button"].ainvoke(
            {"step": 4, "target_id": 12, "thought": "시작 버튼을 누른다"}
        )
        await task

        await tools["report_step"].ainvoke(
            {"step": 4, "passed": True, "message": "상점이 열렸다", "thought": "화면이 바뀌었다"}
        )
        await tools["reply_to_operator"].ainvoke(
            {"step": 4, "message": "상점을 확인했습니다", "thought": "질문에 답한다"}
        )

        by_type: dict[str, list[int | None]] = {}
        for frame in sent:
            by_type.setdefault(frame["type"], []).append(frame["payload"].get("step"))

        assert MessageType.LOG.value not in by_type
        assert by_type[MessageType.ACTION.value] == [4]
        assert by_type[MessageType.STATUS.value] == [4]
        assert by_type[MessageType.CHAT.value] == [4]

    asyncio.run(run())


def test_reported_steps_accumulate_and_announce_the_last_one() -> None:
    async def run() -> None:
        _, state, tools, sent = make(total_steps=2)

        first = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "상점이 열렸다", "thought": "골드 표시를 봤다"}
        )
        assert "1 step(s) left" in first

        second = await tools["report_step"].ainvoke(
            {"step": 2, "passed": False, "message": "골드가 줄지 않았다", "thought": "구매가 안 먹혔다"}
        )
        assert "last step" in second

        assert [item.passed for item in state.step_results] == [True, False]
        statuses = [frame for frame in sent if frame["type"] == MessageType.STATUS.value]
        assert statuses[0]["payload"]["status"] == "COMPLETED"
        assert statuses[1]["payload"]["status"] == "FAILED"

    asyncio.run(run())


def test_a_reported_issue_goes_out_as_an_issue_frame_with_its_evidence() -> None:
    """The far side reads `title` and `severity` and stores the rest as detail,
    so both the display field and the ladder value are pinned here."""

    async def run() -> None:
        _, state, tools, sent = make(total_steps=1)

        result = await tools["report_issue"].ainvoke(
            {
                "step": 2,
                "severity": "MAJOR",
                "title": "상점에서 산 아이템이 인벤토리에 없다",
                "expected": "구매 후 인벤토리에 아이템이 추가된다",
                "actual": "골드만 줄고 인벤토리는 그대로다",
                "reproduction": ["상점을 연다", "회복약을 산다", "인벤토리를 연다"],
                "thought": "구매 처리는 됐는데 지급이 빠졌다",
            }
        )

        issues = [frame for frame in sent if frame["type"] == MessageType.ISSUE.value]
        assert len(issues) == 1
        payload = issues[0]["payload"]
        assert payload["title"] == "상점에서 산 아이템이 인벤토리에 없다"
        assert payload["severity"] == "MAJOR"
        assert payload["step"] == 2
        assert payload["reproduction"][0] == "상점을 연다"
        assert state.issues_attempted == 1
        assert "9 issue(s) left" in result

    asyncio.run(run())


def test_a_severity_off_the_ladder_files_nothing_at_all() -> None:
    """Orchestration drops such a frame without answering, so the agent would
    believe it had reported the defect. The check has to be on this side."""

    async def run() -> None:
        _, state, tools, sent = make(total_steps=1)

        # 먼저 유효한 보고를 하나 해 둔다: 그 뒤의 오타가 앞의 성공에 묻히면 안 된다.
        await tools["report_issue"].ainvoke(
            {
                "step": 1,
                "severity": "MINOR",
                "title": "제목이 잘린다",
                "expected": "제목이 다 보인다",
                "actual": "끝이 잘린다",
                "reproduction": ["타이틀 화면을 연다"],
                "thought": "레이아웃 문제",
            }
        )
        result = await tools["report_issue"].ainvoke(
            {
                "step": 2,
                "severity": "SEVERE",
                "title": "게임이 멈춘다",
                "expected": "계속 진행된다",
                "actual": "멈춘다",
                "reproduction": ["2스텝을 진행한다"],
                "thought": "심각해 보인다",
            }
        )

        assert "not a severity" in result
        assert "BLOCKER/CRITICAL/MAJOR/MINOR/TRIVIAL" in result
        # 프레임은 유효했던 첫 건 하나뿐이고, 실패한 호출은 예산도 쓰지 않는다.
        assert len([frame for frame in sent if frame["type"] == MessageType.ISSUE.value]) == 1
        assert state.issues_attempted == 1

    asyncio.run(run())


def test_a_blank_title_files_nothing_either() -> None:
    """The far side drops a title-less frame as silently as a bad severity, so the
    other required field needs the same guard."""

    async def run() -> None:
        _, state, tools, sent = make(total_steps=1)

        result = await tools["report_issue"].ainvoke(
            {
                "step": 1,
                "severity": "MAJOR",
                "title": "   ",
                "expected": "된다",
                "actual": "안 된다",
                "reproduction": ["연다"],
                "thought": "제목을 빠뜨렸다",
            }
        )

        assert "needs a title" in result
        assert [frame for frame in sent if frame["type"] == MessageType.ISSUE.value] == []
        assert state.issues_attempted == 0

    asyncio.run(run())


def test_the_issue_budget_stops_at_its_cap() -> None:
    async def run() -> None:
        sent: list[dict] = []

        async def send(frame: dict) -> None:
            sent.append(frame)

        channel = QaRunChannel(qa_try_id=7, send=send)
        state = QaRunState(total_steps=1)
        arch = default_resolved_arch().model_copy(update={"max_issues_per_run": 1})
        tools = {tool.name: tool for tool in build_tools(channel, state, arch=arch)}

        first = await tools["report_issue"].ainvoke(
            {
                "step": 1,
                "severity": "BLOCKER",
                "title": "첫 번째",
                "expected": "된다",
                "actual": "안 된다",
                "reproduction": ["연다"],
                "thought": "치명적",
            }
        )
        second = await tools["report_issue"].ainvoke(
            {
                "step": 1,
                "severity": "BLOCKER",
                "title": "두 번째",
                "expected": "된다",
                "actual": "안 된다",
                "reproduction": ["연다"],
                "thought": "또 있다",
            }
        )

        assert "0 issue(s) left" in first
        assert "all 1 issues" in second
        assert len([frame for frame in sent if frame["type"] == MessageType.ISSUE.value]) == 1
        # 상한은 툴 설명에도 드러나야 한다 — 모르면 배분할 수 없다.
        assert "You may file 1 of these" in tools["report_issue"].description

    asyncio.run(run())


def test_a_failed_step_is_told_which_step_comes_next() -> None:
    """A failure is where the loop stops on its own, so the way on is spelled out."""

    async def run() -> None:
        _, _, tools, _ = make(total_steps=3)

        result = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": False,
                "message": "상점이 열리지 않았다",
                "thought": "클릭했지만 화면이 그대로다",
            }
        )

        assert "continue with step 2" in result
        assert "not a reason to stop" in result

    asyncio.run(run())


def test_closing_over_unreported_steps_is_pushed_back_on_once() -> None:
    """The steps never attempted are the ones the run was opened to find out about.

    The push-back has to be a one-off: a game that has stopped answering must
    still be able to close, so the second call closes whatever the state.
    """

    async def run() -> None:
        _, state, tools, sent = make(total_steps=3)
        await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "ok", "thought": "통과"}
        )

        pushback = await tools["finish_run"].ainvoke(
            {"passed": True, "summary": "그만하겠다", "thought": "여기까지만 한다"}
        )

        assert not state.finished
        assert "2, 3" in pushback
        assert not [frame for frame in sent if frame["payload"].get("result")]

        await tools["finish_run"].ainvoke(
            {"passed": False, "summary": "진행 불가", "thought": "게임이 응답하지 않는다"}
        )

        assert state.finished
        terminal = sent[-1]["payload"]
        assert terminal["result"] == "FAILED"
        assert terminal["summary"]["steps"]["failed"] == 2

    asyncio.run(run())


def test_finish_run_reports_the_tally() -> None:
    async def run() -> None:
        _, state, tools, sent = make(total_steps=2)
        await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "ok", "thought": "통과"}
        )
        await tools["report_step"].ainvoke(
            {"step": 2, "passed": False, "message": "no", "thought": "실패"}
        )

        await tools["finish_run"].ainvoke(
            {"passed": False, "summary": "1/2 통과", "thought": "두 번째가 실패했다"}
        )

        assert state.finished
        terminal = sent[-1]["payload"]
        assert terminal["result"] == "FAILED"
        assert terminal["summary"]["steps"]["passed"] == 1
        assert terminal["summary"]["steps"]["failed"] == 1

    asyncio.run(run())


def test_two_tier_summary_case_verdict_is_its_verification_step() -> None:
    """2단 판정: 모든 스텝에 성공/실패, TC 판정 = 그 구간 검증(마지막) 스텝 판정.

    중간 스텝이 실패해도 검증 스텝이 통과하면 그 TC는 통과다 — 그리고 중간 실패는
    steps에 그대로 남아 보인다(사용자 요구).
    """

    async def run() -> None:
        # TC 12 = 스텝 1,2,3(3=검증). standalone 4. TC 20 = 스텝 5(검증).
        step_meta = [(12, False), (12, False), (12, True), (None, False), (20, True)]
        _, state, tools, sent = make(total_steps=5)
        state.step_meta = step_meta
        verdicts = [
            (1, True, "did a1"),
            (2, False, "a2 안됨"),   # 중간 실패
            (3, True, "기대결과 나옴"),  # 검증 통과 → TC12 통과
            (4, True, "did a4"),
            (5, False, "기대결과 안나옴"),  # 검증 실패 → TC20 실패
        ]
        for step, passed, message in verdicts:
            await tools["report_step"].ainvoke(
                {"step": step, "passed": passed, "message": message, "thought": "t"}
            )

        await tools["finish_run"].ainvoke(
            {"passed": False, "summary": "요약", "thought": "종합"}
        )

        summary = sent[-1]["payload"]["summary"]
        # 모든 스텝이 보인다(중간 실패 포함).
        assert summary["steps"]["total"] == 5
        assert summary["steps"]["passed"] == 3
        assert summary["steps"]["failed"] == 2
        # TC 판정은 검증 스텝에서 파생.
        cases = {c["case_id"]: c for c in summary["cases"]["items"]}
        assert summary["cases"]["total"] == 2
        assert cases[12]["passed"] is True   # 중간(2) 실패했지만 검증(3) 통과
        assert cases[12]["steps"] == [1, 2, 3]
        assert cases[12]["verify_step"] == 3
        assert cases[20]["passed"] is False

    asyncio.run(run())


def test_verdict_tools_write_no_thought_row_of_their_own() -> None:
    """판정 tool 들도 제 이유를 LOG 로 남기지 않는다(ARTEL-609).

    report_step, finish_run, reply_to_operator 는 액션을 내지 않으므로, 종전에는 이
    LOG 한 줄이 "왜 불렸나"를 말하는 유일한 자리였다. 지금은 러너가 호출마다 내는 TOOL
    프레임이 그 자리를 맡고, 이 tool 들은 제 결과 프레임만 낸다.
    """

    async def run() -> None:
        _, _, tools, sent = make(total_steps=1)

        await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "상점이 열렸다", "thought": "화면이 바뀌었다"}
        )
        await tools["reply_to_operator"].ainvoke(
            {"message": "상점까지 확인했습니다", "thought": "진행 상황을 묻길래 답한다"}
        )
        await tools["finish_run"].ainvoke(
            {"passed": True, "summary": "전부 통과", "thought": "모든 단계가 통과했다"}
        )

        assert logs(sent) == []
        # 판정 자체는 그대로 나간다. 사라진 것은 이유를 적던 LOG 줄뿐이다.
        assert [frame["type"] for frame in sent] == [
            MessageType.STATUS.value,
            MessageType.CHAT.value,
            MessageType.STATUS.value,
        ]

    asyncio.run(run())


# --- what the model is handed, before any of it is called ---------------------


def test_the_agent_is_offered_exactly_these_tools() -> None:
    """Names are the API between the prompt and the code, and `@tool` derives them.

    A tool renamed by renaming its function reads as a harmless refactor and is
    not one: the system prompt names tools in prose, and a run whose prompt calls
    for a tool the model was not given is a run that stalls on step one.
    """
    _, _, tools, _ = make()

    assert set(tools) == {
        "observe_scene",
        "inspect_object",
        "search_knowledge",
        "record_knowledge",
        "update_knowledge",
        "forget_knowledge",
        "link_knowledge",
        "unlink_knowledge",
        "expand_knowledge",
        "include_screen_selector",
        "exclude_screen_selector",
        "click_button",
        "enter_text",
        "press_key",
        "move_pointer",
        "click_at",
        "double_click_at",
        "hold_mouse_button",
        "release_mouse_button",
        "hold_key",
        "release_key",
        "set_input_axis",
        "set_input_button",
        "drag_pointer",
        "pause_game_time",
        "resume_game_time",
        "reset_game",
        "wait_for_operator",
        "report_step",
        "report_issue",
        "finish_run",
        "reply_to_operator",
        "capture_screen",
    }


def test_every_tool_reaches_the_model_described() -> None:
    """The tool list IS documentation; a blank entry is a tool nobody can use."""
    _, _, tools, _ = make()

    for name, tool in tools.items():
        assert tool.description.strip(), f"{name} reaches the model with no description"


def test_the_capture_tool_states_its_own_budget() -> None:
    """The cap has to be in the description, not only in the refusal.

    An agent that learns the limit by hitting it has already spent it. The
    description is built from MAX_CAPTURES_PER_RUN for exactly this reason, so
    the number cannot drift away from the one the tool enforces.
    """
    _, _, tools, _ = make()

    assert str(MAX_CAPTURES_PER_RUN) in tools["capture_screen"].description


def test_every_tool_takes_a_thought() -> None:
    """The timeline is built out of `thought`; a tool without one logs nothing."""
    _, _, tools, _ = make()

    for name, tool in tools.items():
        assert "thought" in tool.args, f"{name} would act without recording why"


# --- citations (ARTEL-294) ----------------------------------------------------


def test_a_verdict_without_citations_still_goes_out() -> None:
    """The default has to be a working call.

    A model that never fills the field must produce exactly the run it produced
    before the field existed — the metric is worth nothing if adding it changed
    how runs behave.
    """

    async def run() -> None:
        _, _, tools, sent = make(total_steps=1)

        await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "상점이 열렸다", "thought": "화면이 바뀌었다"}
        )

        payload = [f for f in sent if f["type"] == MessageType.STATUS.value][0]["payload"]
        assert payload["used_knowledge_ids"] == []
        assert payload["rejected_knowledge_id_count"] == 0

    asyncio.run(run())


def test_a_verdict_carries_the_knowledge_it_rested_on() -> None:
    async def run() -> None:
        _, state, tools, sent = make(total_steps=1)
        state.knowledge_seen["42"] = "점프는 스페이스바"

        await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "스페이스바로 점프했다",
                "thought": "지식이 말한 조작이 맞았다",
                "used_knowledge_ids": ["42"],
            }
        )

        payload = [f for f in sent if f["type"] == MessageType.STATUS.value][0]["payload"]
        assert payload["used_knowledge_ids"] == ["42"]
        # 스텝 판정은 런을 끝내지 않는다. 인용이 그 규칙을 바꾸면 안 된다.
        assert payload["result"] is None

    asyncio.run(run())


def test_an_id_this_run_never_saw_is_dropped_and_counted() -> None:
    """The hallucinated-citation rate is itself a comparison between models.

    Dropped in silence, a model that invents ids would score exactly like one
    that does not — so the count rides on the frame and the agent is told.
    """

    async def run() -> None:
        _, state, tools, sent = make(total_steps=1)
        state.knowledge_seen["42"] = "본 것"

        result = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "됐다",
                "thought": "판정",
                "used_knowledge_ids": ["42", "999", "없는-id"],
            }
        )

        payload = [f for f in sent if f["type"] == MessageType.STATUS.value][0]["payload"]
        assert payload["used_knowledge_ids"] == ["42"]
        assert payload["rejected_knowledge_id_count"] == 2
        # 판정 자체는 기록됐다는 것과, 무엇이 빠졌는지가 둘 다 모델에게 가야 한다.
        assert "Recorded." in result
        assert "not entries this run has been shown" in result

    asyncio.run(run())


def test_a_search_says_which_step_asked() -> None:
    """`step` was taken by the tool and never sent, which is why every
    `knowledge_usage` row ever written has a null one."""

    async def run() -> None:
        channel, _, tools, sent = make(total_steps=1)

        task = asyncio.create_task(
            tools["search_knowledge"].ainvoke(
                {"step": 3, "thought": "규칙을 확인한다", "query": "점프"}
            )
        )
        for _ in range(50):
            searches = [f for f in sent if f["type"] == MessageType.KNOWLEDGE_SEARCH.value]
            if searches:
                break
            await asyncio.sleep(0)
        task.cancel()

        assert searches[0]["payload"]["step"] == 3

    asyncio.run(run())


# --- 판독만 흐를 때의 도구 결과 (ARTEL-516) ---------------------------------


def reading(reading_id: int = 1, whole: bool = True, objects: list | None = None) -> dict:
    return {
        "type": "PULSE",
        "payload": {
            "schema": 2,
            "reading": reading_id,
            "scene": "Lobby",
            "whole": whole,
            "active": objects or [],
            "deactive": [],
            "changed": [],
        },
    }


def start_button(label: str = "Start") -> dict:
    return {
        "selector": "StartButton[1]",
        "id": -101,
        "rect": {"x": 860, "y": 600, "w": 200, "h": 60},
        "offers": {"clicks": [{"event": "onClick", "method": "TitleSceneManager.StartGame"}]},
        "members": [
            {"on": "TitleSceneManager", "member": "Label", "value": label, "asked": True}
        ],
    }


def test_observe_does_not_say_the_game_was_silent_when_readings_are_flowing() -> None:
    """`GAME_STATE` 가 꺼진 빌드에서 도구가 매 턴 거짓말하던 자리다.

    판독은 멀쩡히 흐르는데 도착 판정이 `scene.frames` 만 봐서, 게임이 답했는데도
    "답하지 않았다" 를 읽었다. 로컬 실측에서 런이 그래도 통과한 것은 러너가 매 턴 끝에
    상시 블록을 붙이고 에이전트가 도구 결과를 무시했기 때문이다 — 통과했다고 해서
    거짓말이 값이 없는 것은 아니다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_pulse(reading(objects=[start_button()]))

        answer_text = await tools["observe_scene"].ainvoke({"step": 1, "thought": "본다"})

        assert "did not answer" not in answer_text
        # 판독 블록이 실렸다. 조준값과 가능한 조작까지(ARTEL-512).
        assert "StartButton[1]" in answer_text
        assert "id=-101" in answer_text
        assert "TitleSceneManager.StartGame" in answer_text
        # 물어보지 않았다.
        assert actions(sent) == []

    asyncio.run(run())


def test_an_action_result_carries_the_reading_that_followed_it() -> None:
    """액션 도구의 결과에 판독 블록이 실린다."""

    async def run() -> None:
        channel, _, tools, sent = make(timeout=1.0)
        channel.on_pulse(reading(objects=[start_button()]))

        async def reply() -> None:
            for _ in range(50):
                if actions(sent):
                    break
                await asyncio.sleep(0)
            channel.on_action_result(
                {
                    "correlationId": actions(sent)[-1]["messageId"],
                    "payload": {"results": [{"id": 1, "success": True}]},
                }
            )
            channel.on_pulse(
                reading(reading_id=2, whole=False, objects=[start_button(label="Loading…")])
            )

        asyncio.create_task(reply())
        body = await tools["click_button"].ainvoke(
            {"step": 1, "thought": "시작을 누른다", "target_id": -101}
        )

        assert "did not arrive" not in body
        assert "button_click: ok" in body
        assert "Loading…" in body, "액션 뒤의 판독이 실렸다"

    asyncio.run(run())


def test_a_still_screen_is_reported_as_still_not_as_silence() -> None:
    """아무것도 안 움직이면 그렇게 말하고, 화면은 그대로 그린다.

    SDK 가 움직인 것 없는 판독을 안 보내므로 이 경우가 흔하다 — 클릭이 아무 일도 하지
    않은 스텝이 그렇고, 그것이야말로 판정하려는 것이다. 여기서 화면을 감추면 판정할
    근거가 사라진다.
    """

    async def run() -> None:
        channel, _, tools, sent = make(timeout=1.0)
        channel.on_pulse(reading(objects=[start_button()]))

        async def reply() -> None:
            for _ in range(50):
                if actions(sent):
                    break
                await asyncio.sleep(0)
            channel.on_action_result(
                {
                    "correlationId": actions(sent)[-1]["messageId"],
                    "payload": {"results": [{"id": 1, "success": True}]},
                }
            )
            # 판독은 오지 않는다. 화면이 그대로다.

        asyncio.create_task(reply())
        body = await tools["click_button"].ainvoke(
            {"step": 1, "thought": "시작을 누른다", "target_id": -101}
        )

        assert "did not arrive" not in body
        assert "Nothing on the screen moved." in body
        assert "StartButton[1]" in body, "그대로인 화면도 보여 준다"

    asyncio.run(run())


def test_a_game_that_reports_nothing_is_told_so() -> None:
    """두 채널 다 조용하면 그렇게 말한다. 이 판정을 무디게 만들지 않았다.

    묻지 않게 된 뒤에도 "안 왔다" 는 여전히 값이다 — 다만 이제 그것은 "물었는데 답이
    없다" 가 아니라 "게임이 화면을 아예 보고하지 않는다" 를 뜻하고, 문구가 그렇게 말한다.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, results=[{"id": 1, "success": True}])
        body = await tools["click_button"].ainvoke(
            {"step": 1, "thought": "누른다", "target_id": -101}
        )

        assert "button_click: ok" in body, "액션 자체는 답했다"
        assert "not reporting the screen" in body
        assert "scan_scene" not in json.dumps(sent)

    asyncio.run(run())


def _with_a_screen():
    """판독이 하나 도착한 채널. 판독이 유일한 출처인 지금의 모양으로."""
    from app.qa.pulse import PulseReading

    channel, state, tools, sent = make()
    channel.scene.pulse.apply(
        PulseReading.model_validate(
            {
                "schema": 2,
                "reading": 1,
                "frame": 100,
                "scene": "TurnBattleScene",
                "whole": True,
                "statics": [],
                "deactive": [],
                "changed": [],
                "active": [
                    {
                        "selector": "Card(Clone)[16]",
                        "id": -12134,
                        "members": [
                            {"member": "cardType", "value": "Fire", "on": "Cards.Card"}
                        ],
                    }
                ],
            }
        )
    )
    return channel, state, tools, sent


def test_판정_뒤_턴에도_화면이_있다() -> None:
    """`report_step` 다음 턴이 다음 스텝을 정하는 자리다. 거기 화면이 없으면
    에이전트가 눈감고 넘어간다.

    종전에는 꼬리가 도구와 무관하게 매 턴 화면을 줘서 이 구멍이 없었다. ARTEL-621 이
    그 꼬리를 없앤 것은 옳았지만 — 프롬프트 접두를 매 턴 깨뜨려 캐시를 못 쓰게 하고
    있었다 — 도구 결과가 화면을 싣는지는 보지 않았다(ARTEL-635)."""

    async def run() -> None:
        _channel, _state, tools, _sent = _with_a_screen()

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "확인함", "thought": "판정"}
        )

        assert "Card(Clone)[16]" in answered, answered

    asyncio.run(run())


def test_지식_검색은_화면을_안_들고_온다() -> None:
    """ARTEL-180 이 정한 것이고 그 논거가 지금도 산다 — 검색은 화면을 바꾸지 않으므로
    화면을 돌려주면 문맥을 다시 쓰는 일이다. 델타가 "마지막 행위 이후"라, 검색을 두 번
    하면 두 번째가 첫 번째와 같은 것을 반복한다."""

    async def run() -> None:
        _channel, _state, tools, _sent = _with_a_screen()

        # 답이 안 와도 좋다. 검사하는 것은 돌아온 문자열에 화면이 없다는 것뿐이다.
        answered = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "찾아본다", "query": "조합 규칙"}
        )

        assert "Card(Clone)[16]" not in answered

    asyncio.run(run())


def test_화면은_한_번만_실린다() -> None:
    """화면을 붙이는 자리가 하나여야 한다는 것의 나머지 반쪽이다.

    ARTEL-635 가 공통 지점을 만들면서 종전에 자기가 그리던 도구들의 그리기를 안 걷어냈다.
    판독이 유일한 출처인 지금 `render` 는 워터마크가 아니라 **마지막 행위**를 경계로 삼으므로,
    같은 결과 안에서 두 번째 호출이 첫 번째와 똑같은 것을 낸다 — 액션 하나의 결과에 판독
    블록이 두 번 실렸다.
    """

    async def run() -> None:
        _channel, _state, tools, _sent = _with_a_screen()
        looked = await tools["observe_scene"].ainvoke({"step": 1, "thought": "본다"})
        assert looked.count("<<pulse>>") == 1, looked

        channel, _, action_tools, sent = make(timeout=1.0)
        channel.on_pulse(reading(objects=[start_button()]))

        async def reply() -> None:
            for _ in range(200):
                if actions(sent):
                    break
                await asyncio.sleep(0)
            channel.on_action_result(
                {
                    "correlationId": actions(sent)[-1]["messageId"],
                    "payload": {"results": [{"id": 1, "success": True}], "frame": 100},
                }
            )
            channel.on_pulse(
                reading(reading_id=2, whole=False, objects=[start_button(label="Loading…")])
            )

        asyncio.create_task(reply())
        acted = await action_tools["click_button"].ainvoke(
            {"step": 1, "thought": "누른다", "target_id": -101}
        )
        assert acted.count("<<pulse>>") == 1, acted

    asyncio.run(run())


def test_마지막_스텝_판정에_지식을_남기라고_말한다() -> None:
    """도구는 있는데 안 쓴다 — 실측으로 83턴 런에서 `record_knowledge` 0회였다.

    기록은 이번 런의 판정에 아무것도 안 보태므로 비용만 있는 행동이고, 무엇보다 적을 순간이
    흐름 안에 없었다. 마지막 스텝을 판정한 자리가 그 순간이다 — 런 전체가 아직 앞에 있고
    판정은 끝났다(ARTEL-667).
    """

    async def run() -> None:
        _channel, _state, tools, _sent = make()

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "확인함", "thought": "판정"}
        )

        assert "That was the last step" in answered, answered
        assert "record_knowledge" in answered

    asyncio.run(run())


def test_이미_남긴_런에게는_무엇을_남겼는지_되짚게_한다() -> None:
    """한 줄 적은 것과 그 런이 알아낸 것을 다 적은 것은 다르다(ARTEL-648).

    ARTEL-667 의 문구는 아무것도 안 적은 런에만 붙었다. 이미 적은 런에게는 시키는 대신
    지금까지 몇 개를 남겼는지 세어 주고 나머지를 되짚게 한다.
    """

    async def run() -> None:
        _channel, state, tools, _sent = make()
        state.knowledge_records_attempted = 1

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "확인함", "thought": "판정"}
        )

        assert "That was the last step" in answered
        assert "Knowledge recording attempts this run: 1" in answered, answered
        assert "Check whether each one succeeded" in answered
        assert "record_knowledge" in answered

    asyncio.run(run())


def test_실패한_스텝이_있는데_issue_가_없으면_그것을_짚는다() -> None:
    """실측으로 83턴 런에서 `report_issue` 0회였다(ARTEL-648).

    실패로 판정한 스텝이 있는데 issue 가 하나도 없으면 그 런은 무엇이 왜 실패했는지를
    아무 데도 안 남긴 것이다. 묻는 근거는 그 실패한 스텝이고, 그것을 문구에 담아야
    agent 가 무엇에 대해 답할지 안다.
    """

    async def run() -> None:
        _channel, _state, tools, _sent = make()

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": False, "message": "안 열렸다", "thought": "판정"}
        )

        assert "Steps judged failed this run: 1" in answered, answered
        assert "Issue reports sent: none" in answered
        assert "report_issue" in answered

    asyncio.run(run())


def test_실패한_스텝이_없으면_결함은_묻지_않는다() -> None:
    """근거 없이 물으면 agent 가 무엇에 대해 답할지 모르고, 그래도 답하려고 아무거나
    적으면 다음 런에 도움이 안 되는 줄만 쌓인다."""

    async def run() -> None:
        _channel, _state, tools, _sent = make()

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "확인함", "thought": "판정"}
        )

        assert "That was the last step" in answered, answered
        assert "report_issue" not in answered
        assert "Steps judged failed" not in answered

    asyncio.run(run())


def test_이미_낸_issue_가_있어도_남은_실패를_되짚게_한다() -> None:
    """실패한 스텝 수와 낸 issue 수를 나란히 세어 준다. 하나 냈다고 나머지 실패가
    덮이지는 않는다."""

    async def run() -> None:
        _channel, state, tools, _sent = make()
        state.issues_attempted = 1

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": False, "message": "안 열렸다", "thought": "판정"}
        )

        assert "Steps judged failed this run: 1" in answered, answered
        assert "Issue reports sent: 1" in answered
        assert "report_issue" in answered

    asyncio.run(run())


def test_적을_것이_없다는_것도_답이라고_말한다() -> None:
    """억지로 적게 만들면 지식창고가 다음 런에 도움이 안 되는 줄로 채워진다. 묻되
    강요하지 않고, 런을 닫으라는 말은 그대로 남는다."""

    async def run() -> None:
        _channel, _state, tools, _sent = make()

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": False, "message": "안 열렸다", "thought": "판정"}
        )

        assert "Nothing to write is an answer" in answered, answered
        assert "finish the run" in answered

    asyncio.run(run())


def test_중간_스텝에서는_말하지_않는다() -> None:
    """말할 자리는 마지막 판정 하나다. 매 스텝마다 붙이면 표가 뜻을 잃는다."""

    async def run() -> None:
        _channel, _state, tools, _sent = make(total_steps=3)

        answered = await tools["report_step"].ainvoke(
            {"step": 1, "passed": False, "message": "안 열렸다", "thought": "판정"}
        )

        assert "step(s) left" in answered, answered
        assert "record_knowledge" not in answered
        assert "report_issue" not in answered

    asyncio.run(run())


def test_current_scene_을_청하면_전량이_온다() -> None:
    """주소를 모르면 `inspect_object` 로도 못 묻는다. 실제 런에서 `Card(Clone)` 을 찍어서
    묻고 없다는 답을 받았다(ARTEL-673)."""

    async def run() -> None:
        _channel, _state, tools, _sent = _with_a_screen()
        await tools["observe_scene"].ainvoke({"step": 1, "thought": "본다"})

        again = await tools["observe_scene"].ainvoke({"step": 1, "thought": "또 본다"})
        assert "Card(Clone)[16]" not in again, again

        page = await tools["observe_scene"].ainvoke(
            {"step": 1, "thought": "전부 본다", "current_scene": True}
        )
        assert "Card(Clone)[16]" in page, page
        assert page.count("<<pulse>>") == 1, "화면이 두 번 실리면 안 된다"

    asyncio.run(run())


def test_current_scene_이_행위_경계를_안_옮긴다() -> None:
    """관측은 행위가 아니다. 전량을 봤다고 그 사이 무엇이 쌓였는지를 잊어도 되는 것이 아니다."""

    async def run() -> None:
        _channel, state, tools, _sent = _with_a_screen()
        state.last_action_frame = 99

        await tools["observe_scene"].ainvoke(
            {"step": 1, "thought": "전부 본다", "current_scene": True}
        )

        assert state.last_action_frame == 99

    asyncio.run(run())
