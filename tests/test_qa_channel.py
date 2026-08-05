import asyncio

import pytest

from app.qa.channel import QaCancelled, QaRunChannel, with_operator_messages
from app.qa.envelope import JsonRpcAction, MessageType


def make_channel(timeout: float = 30.0) -> tuple[QaRunChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout)
    return channel, sent


def scene_frame(scene: str = "Lobby", observables: dict | None = None) -> dict:
    return {
        "type": "GAME_STATE",
        "payload": {"scene": scene, "interactables": [], "observables": observables or {}},
    }


def test_looking_goes_out_as_a_scan_scene_action() -> None:
    """One path to the scene: the SDK's JSON-RPC method, same as every other action.

    There used to be a second — a REQUEST_GAME_STATE frame that Orchestration
    turned into a top-level GET_GAME_STATE — which the SDK only keeps as an
    alias, and which made the timeline log this envelope instead of the frame
    that actually reached the game.
    """

    async def run() -> None:
        channel, sent = make_channel()

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_game_state(scene_frame(observables={"Score": {"value": 1}}))
            channel.on_action_result(
                {"correlationId": sent[0]["messageId"], "payload": {"results": []}}
            )

        asyncio.create_task(answer())
        arrived = await channel.look(0.0, "look")

        assert arrived is True
        assert sent[0]["type"] == MessageType.ACTION.value
        assert sent[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "scan_scene", "params": []}
        ]
        # The memory is updated on the way in, so the tool can render a diff.
        assert channel.scene.observables["Score"].current == 1

    asyncio.run(run())


def test_a_scene_transition_counts_as_a_scene_arriving() -> None:
    """The freshness check cannot key on `updates`.

    `SceneMemory` resets that counter with the scene, so a run that just moved
    from one scene to another would have the transition reported as the game
    having stayed silent — the one moment the agent most needs to see.
    """

    async def run() -> None:
        channel, sent = make_channel()

        async def answer(scene: str) -> None:
            await asyncio.sleep(0)
            channel.on_game_state(scene_frame(scene=scene))
            channel.on_action_result(
                {"correlationId": sent[-1]["messageId"], "payload": {"results": []}}
            )

        asyncio.create_task(answer("Lobby"))
        await channel.look(0.0, "look")
        asyncio.create_task(answer("Lobby"))
        await channel.look(0.0, "look")

        asyncio.create_task(answer("Shop"))
        assert await channel.look(0.0, "look") is True
        assert channel.scene.scene == "Shop"
        # The per-scene counter did restart; the run-wide one did not.
        assert channel.scene.updates == 1
        assert channel.scene.frames == 3

    asyncio.run(run())


def test_looking_reports_false_when_no_scene_arrives() -> None:
    """No answer is a value, not an exception — the agent decides what to do."""

    async def run() -> None:
        channel, _ = make_channel(timeout=0.05)
        assert await channel.look(0.0, "look") is False

    asyncio.run(run())


def test_action_result_with_a_foreign_correlation_is_ignored() -> None:
    async def run() -> None:
        channel, sent = make_channel(timeout=0.05)

        async def answer() -> None:
            await asyncio.sleep(0)
            # Belongs to some earlier action; must not resolve this wait.
            channel.on_action_result({"correlationId": "someone-else", "payload": {}})

        asyncio.create_task(answer())
        result = await channel.dispatch_actions(
            [JsonRpcAction(id=1, method="button_click", params=[1])], "tap"
        )

        assert result is None
        assert sent[0]["type"] == MessageType.ACTION.value

    asyncio.run(run())


def test_action_result_matching_the_pending_action_resolves() -> None:
    async def run() -> None:
        channel, sent = make_channel()

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_action_result(
                {
                    "correlationId": sent[0]["messageId"],
                    "payload": {"results": [{"id": 1, "status": "SUCCEEDED"}]},
                }
            )

        asyncio.create_task(answer())
        result = await channel.dispatch_actions(
            [JsonRpcAction(id=1, method="button_click", params=[1])], "tap"
        )

        assert result is not None
        assert result.results[0].id == 1

    asyncio.run(run())


def test_cancel_stops_the_next_tool() -> None:
    async def run() -> None:
        channel, _ = make_channel()
        channel.on_cancel()

        with pytest.raises(QaCancelled):
            await channel.look(0.0, "look")

    asyncio.run(run())


def test_operator_messages_are_drained_once() -> None:
    channel, _ = make_channel()
    channel.on_chat({"payload": {"message": "메뉴로 가"}})

    assert channel.drain_operator_messages() == ["메뉴로 가"]
    assert channel.drain_operator_messages() == []


def test_draining_does_not_erase_what_the_operator_said() -> None:
    """Delivery is once; the record is for the whole run.

    Once drained, an instruction exists only inside the text of one tool result,
    and compaction replaces exactly that text. "It applies from now on" then quietly
    stops being true. `render_progress_ledger` restates this list afterwards.
    """
    channel, _ = make_channel()
    channel.on_chat({"payload": {"message": "메뉴로 가"}})
    channel.on_chat({"payload": {"message": "천천히"}})

    channel.drain_operator_messages()

    assert channel.operator_instructions == ["메뉴로 가", "천천히"]


def test_waiting_returns_as_soon_as_the_operator_speaks() -> None:
    async def run() -> None:
        channel, _ = make_channel()

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_chat({"payload": {"message": "상점으로 가"}})

        asyncio.create_task(answer())
        assert await channel.wait_for_operator(30.0) == ["상점으로 가"]
        # Handed over, so the next tool result must not repeat them.
        assert channel.drain_operator_messages() == []

    asyncio.run(run())


def test_waiting_finds_a_message_that_arrived_before_it() -> None:
    """The operator does not wait to be asked, and their words must not be lost."""

    async def run() -> None:
        channel, _ = make_channel()
        channel.on_chat({"payload": {"message": "그대로 진행해"}})

        assert await channel.wait_for_operator(30.0) == ["그대로 진행해"]

    asyncio.run(run())


def test_waiting_returns_empty_on_silence() -> None:
    """Timeout is an answer the agent acts on, not an exception."""

    async def run() -> None:
        channel, _ = make_channel()
        assert await channel.wait_for_operator(0.05) == []

    asyncio.run(run())


def test_cancel_wakes_a_wait_instead_of_letting_it_sit() -> None:
    """Nothing else would release it: a wait has no action to cancel."""

    async def run() -> None:
        channel, _ = make_channel()

        async def cancel() -> None:
            await asyncio.sleep(0)
            channel.on_cancel()

        asyncio.create_task(cancel())
        with pytest.raises(QaCancelled):
            await channel.wait_for_operator(30.0)

    asyncio.run(run())


def test_operator_messages_are_appended_to_a_tool_result() -> None:
    assert with_operator_messages("scene: Lobby", []) == "scene: Lobby"

    merged = with_operator_messages("scene: Lobby", ["메뉴로 가"])
    assert "scene: Lobby" in merged
    assert "메뉴로 가" in merged
