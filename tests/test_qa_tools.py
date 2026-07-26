import asyncio

from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


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


def test_observe_returns_the_change_since_the_last_look() -> None:
    async def run() -> None:
        channel, state, tools, _ = make()

        async def answer(value: int) -> None:
            await asyncio.sleep(0)
            channel.on_game_state(scene({"Score": {"value": value}}))

        asyncio.create_task(answer(0))
        await tools["observe_scene"].ainvoke({})

        asyncio.create_task(answer(100))
        second = await tools["observe_scene"].ainvoke({})

        # The second look reports the move, not just the current number.
        assert "Score: 0 → 100" in second

    asyncio.run(run())


def test_observe_says_so_when_the_game_is_silent() -> None:
    """Silence must come back as a value the agent can act on, not an exception."""

    async def run() -> None:
        _, _, tools, _ = make()
        result = await tools["observe_scene"].ainvoke({})
        assert "did not answer" in result

    asyncio.run(run())


def test_operator_message_is_appended_to_the_next_tool_result() -> None:
    async def run() -> None:
        channel, _, tools, _ = make()
        channel.on_chat({"payload": {"message": "메뉴로 가"}})

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_game_state(scene({}))

        asyncio.create_task(answer())
        result = await tools["observe_scene"].ainvoke({})

        assert "메뉴로 가" in result
        # Delivered once; it must not repeat on every later call.
        asyncio.create_task(answer())
        assert "메뉴로 가" not in await tools["observe_scene"].ainvoke({})

    asyncio.run(run())


def test_pressing_a_key_logs_the_reason_and_batches_a_scan() -> None:
    """press_key needs no target, so it works on a screen with nothing clickable."""

    async def run() -> None:
        _, _, tools, sent = make()
        await tools["press_key"].ainvoke(
            {"key_code": "Space", "duration_seconds": 0.5, "thought": "대사를 넘긴다"}
        )

        logged = [f for f in sent if f["type"] == MessageType.LOG.value]
        assert logged[0]["payload"]["message"] == "대사를 넘긴다"

        action = next(f for f in sent if f["type"] == MessageType.ACTION.value)
        methods = [a["method"] for a in action["payload"]["actions"]]
        # The scan rides in the same batch so it cannot answer before the key does.
        assert methods == ["key_click", "scan_scene"]

    asyncio.run(run())


def test_click_reports_the_failure_reason() -> None:
    async def run() -> None:
        channel, _, tools, sent = make()

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_action_result(
                {
                    "correlationId": next(
                        f["messageId"] for f in sent if f["type"] == MessageType.ACTION.value
                    ),
                    "payload": {"results": [{"id": 1, "success": False, "error": "Unknown target id: 999"}]},
                }
            )

        task = asyncio.create_task(answer())
        result = await tools["click_button"].ainvoke({"target_id": 999, "thought": "시작 버튼"})
        await task

        assert "FAILED" in result
        assert "Unknown target id: 999" in result

    asyncio.run(run())


def test_reported_steps_accumulate_and_announce_the_last_one() -> None:
    async def run() -> None:
        _, state, tools, sent = make(total_steps=2)

        first = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "상점이 열렸다"}
        )
        assert "1 step(s) left" in first

        second = await tools["report_step"].ainvoke(
            {"step": 2, "passed": False, "message": "골드가 줄지 않았다"}
        )
        assert "last step" in second

        assert [item.passed for item in state.step_results] == [True, False]
        statuses = [frame for frame in sent if frame["type"] == MessageType.STATUS.value]
        assert statuses[0]["payload"]["status"] == "COMPLETED"
        assert statuses[1]["payload"]["status"] == "FAILED"

    asyncio.run(run())


def test_finish_run_reports_the_tally() -> None:
    async def run() -> None:
        _, state, tools, sent = make(total_steps=2)
        await tools["report_step"].ainvoke({"step": 1, "passed": True, "message": "ok"})
        await tools["report_step"].ainvoke({"step": 2, "passed": False, "message": "no"})

        await tools["finish_run"].ainvoke({"passed": False, "summary": "1/2 통과"})

        assert state.finished
        terminal = sent[-1]["payload"]
        assert terminal["result"] == "FAILED"
        assert terminal["summary"]["passed"] == 1
        assert terminal["summary"]["failed"] == 1

    asyncio.run(run())
