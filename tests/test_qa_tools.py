import asyncio

from app.agents.qa.tools import QaRunState, build_tools
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


def make(total_steps: int = 1, timeout: float = 0.05):
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(
        qa_try_id=7, send=send, scene_timeout=timeout, action_timeout=timeout
    )
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


def test_action_without_a_method_runs_nothing() -> None:
    async def run() -> None:
        _, _, tools, sent = make()
        result = await tools["perform_actions"].ainvoke(
            {"actions": [{"target_id": 1}], "message": "tap"}
        )
        assert "needs a method" in result
        assert sent == []

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
