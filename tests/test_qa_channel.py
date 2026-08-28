import asyncio
import json

import pytest

from app.qa.channel import (
    READING_WAIT_SECONDS,
    QaCancelled,
    QaRunChannel,
    with_operator_messages,
)
from app.qa.envelope import JsonRpcAction, MessageType


def make_channel(timeout: float = 30.0) -> tuple[QaRunChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout, write_timeout=timeout)
    return channel, sent


def scene_frame(scene: str = "Lobby", observables: dict | None = None) -> dict:
    return {
        "type": "GAME_STATE",
        "payload": {"scene": scene, "interactables": [], "observables": observables or {}},
    }


def test_looking_asks_the_game_for_nothing() -> None:
    """`look` 이 프레임을 하나도 안 보낸다.

    종전에는 `scan_scene` 을 실은 ACTION 이 나갔다. 그 액션의 유일한 일이 `GAME_STATE` 를
    만드는 것인데, ARTEL-513 이 그 채널을 끄면 오류를 답하고, 켜 두면 `PollSceneState` 가
    이미 같은 것을 1초마다 스스로 올린다 — 어느 쪽에서도 하는 일이 없다(ARTEL-516).

    두 채널 다 묻지 않고 도착한다는 것이 이 파일이 지킬 계약이다.
    """

    async def run() -> None:
        channel, sent = make_channel(timeout=0.05)
        channel.on_pulse(pulse_frame())

        assert await channel.look(0.0) is True
        assert sent == []

    asyncio.run(run())


def test_a_volunteered_scene_is_enough_to_look_at() -> None:
    """스위치를 되돌린 빌드에서는 폴러가 올린 프레임이 답이다.

    ARTEL-513 은 되돌릴 수 있어야 하고, 되돌리면 `GAME_STATE` 가 묻지 않아도 흐른다.
    그때 그것을 못 본 척하면 이 도구가 거짓말하던 자리로 되돌아간다 — 판독을 세게 된 것과
    같은 이유로 프레임도 여전히 센다.
    """

    async def run() -> None:
        channel, sent = make_channel(timeout=0.05)
        channel.on_game_state(scene_frame(observables={"Score": {"value": 1}}))

        assert await channel.look(0.0) is True
        assert sent == []
        assert channel.scene.observables["Score"].current == 1

    asyncio.run(run())


def test_a_scene_transition_counts_as_a_scene_arriving() -> None:
    """The freshness check cannot key on `updates`.

    `SceneMemory` resets that counter with the scene, so a run that just moved
    from one scene to another would have the transition reported as the game
    having stayed silent — the one moment the agent most needs to see.
    """

    async def run() -> None:
        channel, _ = make_channel(timeout=0.05)

        channel.on_game_state(scene_frame(scene="Lobby"))
        assert await channel.look(0.0) is True
        channel.on_game_state(scene_frame(scene="Arena"))
        assert await channel.look(0.0) is True
        assert channel.scene.updates == 1, "전환이 카운터를 되돌렸다"
        assert channel.scene.frames == 2

    asyncio.run(run())


def test_looking_reports_false_when_nothing_has_ever_arrived() -> None:
    """No answer is a value, not an exception — the agent decides what to do.

    묻지 않게 된 뒤에도 이 판정은 무디어지지 않는다. 한 배치만 기다려 보고 — 런이 막
    시작한 창이 그 모양이다 — 그래도 빈손이면 거짓이다.
    """

    async def run() -> None:
        channel, sent = make_channel(timeout=0.05)
        assert await channel.look(0.0) is False
        assert sent == [], "빈손이어도 묻지는 않는다"

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


def test_two_actions_in_flight_each_get_their_own_answer() -> None:
    """action 을 내는 곳이 둘이다 — 도구와 새 screen 자동 capture(ARTEL-595).

    종전에는 나간 ACTION 하나의 future 를 필드로 들고 있어서, 뒤의 것이 앞의 것을 덮어썼다.
    그러면 앞의 action 은 자기 답이 도착해도 못 받고 타임아웃까지 앉아 있다가 "게임이 답하지
    않았다" 가 된다 — 도구가 방금 누른 버튼이 안 먹혔다고 읽는 자리다.
    """

    async def run() -> None:
        channel, sent = make_channel(timeout=1.0)

        first = asyncio.create_task(
            channel.dispatch_actions([JsonRpcAction(id=1, method="button_click", params=[1])], "tap")
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            channel.dispatch_actions([JsonRpcAction(id=1, method="capture_screen", params=[])], "shot")
        )
        await asyncio.sleep(0)

        # 나중에 나간 것이 먼저 답한다. 순서가 아니라 correlation 이 짝을 정한다.
        channel.on_action_result(
            {"correlationId": sent[1]["messageId"], "payload": {"results": [{"id": 2}]}}
        )
        channel.on_action_result(
            {"correlationId": sent[0]["messageId"], "payload": {"results": [{"id": 1}]}}
        )

        assert (await first).results[0].id == 1
        assert (await second).results[0].id == 2

    asyncio.run(run())


def test_an_uncorrelated_answer_is_dropped_when_two_are_waiting() -> None:
    """옛 orchestration 은 correlation 을 안 싣는다. 하나뿐일 때만 그것으로 친다.

    둘이 떠 있는데 짐작으로 고르면 도구가 자동 capture 의 답을 자기 action 의 결과로 읽는다.
    """

    async def run() -> None:
        channel, _ = make_channel(timeout=0.05)

        first = asyncio.create_task(
            channel.dispatch_actions([JsonRpcAction(id=1, method="button_click", params=[1])], "tap")
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            channel.dispatch_actions([JsonRpcAction(id=1, method="capture_screen", params=[])], "shot")
        )
        await asyncio.sleep(0)

        channel.on_action_result({"payload": {"results": [{"id": 1}]}})

        assert await first is None
        assert await second is None

    asyncio.run(run())


def test_an_uncorrelated_answer_still_resolves_a_lone_action() -> None:
    async def run() -> None:
        channel, _ = make_channel(timeout=1.0)

        pending = asyncio.create_task(
            channel.dispatch_actions([JsonRpcAction(id=1, method="button_click", params=[1])], "tap")
        )
        await asyncio.sleep(0)
        channel.on_action_result({"payload": {"results": [{"id": 1}]}})

        assert (await pending).results[0].id == 1

    asyncio.run(run())


def test_cancel_stops_the_next_tool() -> None:
    async def run() -> None:
        channel, _ = make_channel()
        channel.on_cancel()

        with pytest.raises(QaCancelled):
            await channel.look(0.0)

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


# --- 판독이 흐를 때의 도착 판정 (ARTEL-516) ---------------------------------


def pulse_frame(
    reading: int = 1, scene: str = "Lobby", whole: bool = True, changed: list | None = None
) -> dict:
    """SDK 가 내는 모양 그대로. `payload` 로 한 번 더 감싸지 않는다."""
    return {
        "type": "PULSE",
        "payload": {
            "schema": 2,
            "reading": reading,
            "scene": scene,
            "whole": whole,
            "active": [],
            "deactive": [],
            "changed": changed or [],
        },
    }


def test_looking_waits_one_batch_for_the_very_first_reading() -> None:
    """런이 막 시작한 창. `start_readings` 는 나갔고 첫 배치는 아직이다.

    여기서 안 기다리면 런의 첫 관찰이 언제나 "답하지 않았다" 가 되고, 그 한 턴은 모델
    호출 하나다 — 한 배치를 기다리는 쪽이 싸다. 무한정 기다리지는 않는다.
    """

    async def run() -> None:
        channel, sent = make_channel(timeout=0.05)

        async def first() -> None:
            await asyncio.sleep(0.05)
            channel.on_pulse(pulse_frame())

        asyncio.create_task(first())
        assert await channel.look(0.0) is True
        assert sent == []

    asyncio.run(run())


def test_acting_does_not_ride_a_scan_scene_once_readings_are_flowing() -> None:
    """액션 배치에 `scan_scene` 을 태우지 않는다."""

    async def run() -> None:
        channel, sent = make_channel()
        channel.on_pulse(pulse_frame())

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_pulse(pulse_frame(reading=2, whole=False, changed=["Score"]))
            channel.on_action_result(
                {"correlationId": sent[0]["messageId"], "payload": {"results": []}}
            )

        asyncio.create_task(answer())
        result, arrived = await channel.act_and_look(
            [JsonRpcAction(id=1, method="button_click", params=[-101])], "click"
        )

        assert result is not None
        assert arrived is True
        methods = [action["method"] for action in sent[0]["payload"]["actions"]]
        assert methods == ["button_click"], "꼬리가 붙지 않는다"
        assert "scan_scene" not in json.dumps(sent), "어디에도 나가지 않는다"

    asyncio.run(run())


def test_acting_waits_for_the_reading_that_carries_the_result() -> None:
    """`ACTION_RESULT` 가 먼저 와도 다음 판독까지 기다린다.

    판독은 1초 배치라 액션이 끝난 시점에는 그 결과가 아직 안 나갔을 수 있다. 여기서
    안 기다리면 도구가 액션 **이전**의 화면을 그리고, 그것을 근거로 스텝이 판정된다.
    종전에 `scan_scene` 이 같은 배치 끝에 탄 것도 같은 이유였다.
    """

    async def run() -> None:
        channel, sent = make_channel()
        channel.on_pulse(pulse_frame())

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_action_result(
                {"correlationId": sent[0]["messageId"], "payload": {"results": []}}
            )
            # 결과가 실린 배치는 그 뒤에 나간다.
            await asyncio.sleep(0.05)
            channel.on_pulse(pulse_frame(reading=2, scene="Map", whole=True))

        asyncio.create_task(answer())
        _, arrived = await channel.act_and_look(
            [JsonRpcAction(id=1, method="button_click", params=[-101])], "click"
        )

        assert arrived is True
        assert channel.scene.pulse.scene == "Map", "기다린 판독을 실제로 접었다"

    asyncio.run(run())


def test_a_still_screen_is_not_the_game_failing_to_answer() -> None:
    """움직인 것이 없으면 판독이 아예 안 나온다. 그것을 침묵으로 세지 않는다.

    SDK 는 직전과 같은 판독을 붙들고 보내지 않는다(`Pulse.Take` 의 `settled`). 그래서
    "판독이 안 왔다" 는 게임이 죽었다는 뜻이 아니라 화면이 그대로라는 뜻이고, 부르는
    쪽이 그 둘을 갈라 읽어야 한다. 여기서 지키는 것은 **기다림이 끝난다**는 것이다.
    """

    async def run() -> None:
        channel, sent = make_channel()
        channel.on_pulse(pulse_frame())

        async def answer() -> None:
            await asyncio.sleep(0)
            channel.on_action_result(
                {"correlationId": sent[0]["messageId"], "payload": {"results": []}}
            )

        asyncio.create_task(answer())
        loop = asyncio.get_running_loop()
        started = loop.time()
        result, arrived = await channel.act_and_look(
            [JsonRpcAction(id=1, method="button_click", params=[-101])], "click"
        )
        waited = loop.time() - started

        assert result is not None, "액션 자체는 답했다"
        assert arrived is False, "새로 온 것이 없다"
        # 상한에서 풀린다. 무한히 앉아 있으면 런이 데드라인에서 죽는다.
        assert waited < READING_WAIT_SECONDS * 2

    asyncio.run(run())
