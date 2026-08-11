"""What the tools put on the wire, not just what they return to the model.

A real run showed the gap: every tool answered the agent correctly while the
timeline stayed blind. `observe_scene` logged nothing at all because it took no
`thought`, and every row landed with `step` null because no tool passed one.
These pin the frames themselves — category, step, and the actions inside them.
"""

import asyncio

from app.agents.qa.arch import default_resolved_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.vision import MAX_CAPTURES_PER_RUN
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, MessageType


def make(total_steps: int = 1, timeout: float = 0.05):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout)
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
    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, observables={"Score": {"value": 0}})
        await tools["observe_scene"].ainvoke({"step": 1, "thought": "화면을 본다"})

        answer(channel, sent, observables={"Score": {"value": 100}})
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


def test_observing_goes_out_as_an_action_carrying_only_scan_scene() -> None:
    """One path to the scene, and it is the SDK's own JSON-RPC method.

    `observe_scene` used to send a REQUEST_GAME_STATE frame, which Orchestration
    turned into a top-level GET_GAME_STATE — an alias the SDK keeps only for
    compatibility while naming `scan_scene` as the real method.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, observables={})
        await tools["observe_scene"].ainvoke({"step": 2, "thought": "화면을 본다"})

        assert [frame["type"] for frame in sent] == [
            MessageType.LOG.value,
            MessageType.ACTION.value,
        ]
        assert actions(sent)[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "scan_scene", "params": []}
        ]

    asyncio.run(run())


def test_observing_writes_a_thought_row() -> None:
    """The one tool that used to be silent. Five looks in a real run logged nothing."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, observables={})
        await tools["observe_scene"].ainvoke(
            {"step": 3, "thought": "로딩이 끝났는지 확인한다"}
        )

        assert [(row["payload"]["category"], row["payload"]["message"]) for row in logs(sent)] == [
            (LogCategory.THOUGHT.value, "로딩이 끝났는지 확인한다")
        ]

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


def test_pressing_a_key_logs_the_reason_and_batches_a_scan() -> None:
    """press_key needs no target, so it works on a screen with nothing clickable."""

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["press_key"].ainvoke(
            {"step": 1, "key_code": "Space", "duration_seconds": 0.5, "thought": "대사를 넘긴다"}
        )

        assert logs(sent)[0]["payload"]["message"] == "대사를 넘긴다"

        methods = [a["method"] for a in actions(sent)[0]["payload"]["actions"]]
        # The scan rides in the same batch so it cannot answer before the key does.
        assert methods == ["key_click", "scan_scene"]

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
            {"id": 2, "jsonrpc": "2.0", "method": "scan_scene", "params": []},
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
            {"id": 5, "jsonrpc": "2.0", "method": "scan_scene", "params": []},
        ]

    asyncio.run(run())


def test_pausing_and_resuming_go_out_as_the_time_actions() -> None:
    """Freezing the game is an action like any other, so a scan rides with it.

    Without the scan the agent would freeze the screen and then have to ask for
    it separately — one round trip during which the thing it paused to read may
    already be gone.
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
        ] == [["pause_time", "scan_scene"], ["resume_time", "scan_scene"]]

    asyncio.run(run())


def test_resetting_goes_out_as_the_reset_action() -> None:
    """The scan riding with it is what makes the reset usable.

    Every target id the agent holds dies with the old scene, so a reset that
    came back without a fresh scene would leave it acting on corpses.
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
            "reset_game",
            "scan_scene",
        ]

    asyncio.run(run())


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
        # The reason for stopping the run belongs on the timeline like any other.
        assert logs(sent)[0]["payload"]["message"] == "시나리오에 없는 화면이라 물어본다"
        assert logs(sent)[0]["payload"]["step"] == 2
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
    the LOG, the ACTION, the STATUS and the CHAT all have to carry the number the
    agent named.
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

        assert by_type[MessageType.LOG.value] == [4, 4, 4]
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

        # 근거를 남긴 이유도 타임라인에 있어야 한다.
        assert logs(sent)[-1]["payload"]["message"] == "구매 처리는 됐는데 지급이 빠졌다"

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


def test_verdict_tools_each_write_their_own_thought_row() -> None:
    """A verdict without the reasoning behind it is the row a reviewer cannot use.

    report_step, finish_run and reply_to_operator produce no action, so nothing
    else on the timeline would say why they were called.
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

        assert [row["payload"]["message"] for row in logs(sent)] == [
            "화면이 바뀌었다",
            "진행 상황을 묻길래 답한다",
            "모든 단계가 통과했다",
        ]
        assert {row["payload"]["category"] for row in logs(sent)} == {
            LogCategory.THOUGHT.value
        }

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
        "search_knowledge",
        "record_knowledge",
        "update_knowledge",
        "forget_knowledge",
        "link_knowledge",
        "unlink_knowledge",
        "expand_knowledge",
        "click_button",
        "enter_text",
        "press_key",
        "move_pointer",
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
