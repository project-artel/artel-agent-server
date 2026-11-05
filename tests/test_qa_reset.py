import asyncio

from app.agents.qa.reset import FullResetPolicy
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


def make_channel() -> tuple[QaRunChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return QaRunChannel(qa_try_id=7, send=send), sent


def answer_first_action(channel: QaRunChannel, sent: list[dict]) -> None:
    """첫 액션 결과를 즉시 돌려준다. dispatch가 결과를 기다리므로 30s 타임아웃을 피한다."""

    async def reply() -> None:
        await asyncio.sleep(0)
        channel.on_action_result(
            {"correlationId": sent[0]["messageId"], "payload": {"results": []}}
        )

    asyncio.create_task(reply())


def test_full_reset_dispatches_reset_game() -> None:
    """FullResetPolicy는 시나리오 사이에 reset_game 액션을 내보낸다(첫 씬 리로드).

    `params: []` 단언은 이제 새 생성자 기본값까지 고정한다 — clearPlayerPrefs 를 모르는
    옛 SDK 가 보는 frame 이 지금과 동일해야 한다.
    """

    async def run() -> None:
        channel, sent = make_channel()
        answer_first_action(channel, sent)
        await FullResetPolicy().between_scenarios(channel, completed_index=0, total=2)

        assert sent[0]["type"] == MessageType.ACTION.value
        assert sent[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "reset_game", "params": []}
        ]

    asyncio.run(run())


def test_a_full_reset_can_be_asked_to_clear_player_prefs() -> None:
    """켜면 저장 데이터까지 지우라는 파라미터가 실려 나간다.

    기본값이 끄기라 `DEFAULT_RESET_POLICY` 를 쓰는 run 의 frame 은 그대로다. 이 테스트가
    고정하는 것은 켰을 때의 wire 모양 — SDK 가 읽는 이름은 camelCase 인 `clearPlayerPrefs`
    다.
    """

    async def run() -> None:
        channel, sent = make_channel()
        answer_first_action(channel, sent)
        await FullResetPolicy(clear_player_prefs=True).between_scenarios(
            channel, completed_index=0, total=2
        )

        assert sent[0]["payload"]["actions"] == [
            {
                "id": 1,
                "jsonrpc": "2.0",
                "method": "reset_game",
                "params": [{"clearPlayerPrefs": True}],
            }
        ]

    asyncio.run(run())
