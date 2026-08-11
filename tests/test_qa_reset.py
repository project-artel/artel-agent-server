import asyncio

from app.agents.qa.reset import FullResetPolicy
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


def make_channel() -> tuple[QaRunChannel, list[dict]]:
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    return QaRunChannel(qa_try_id=7, send=send), sent


def test_full_reset_dispatches_reset_game() -> None:
    """FullResetPolicy는 시나리오 사이에 reset_game 액션을 내보낸다(첫 씬 리로드)."""

    async def run() -> None:
        channel, sent = make_channel()

        async def answer() -> None:
            # dispatch가 결과를 기다리므로, 액션 결과를 즉시 돌려줘 30s 타임아웃을 피한다.
            await asyncio.sleep(0)
            channel.on_action_result(
                {"correlationId": sent[0]["messageId"], "payload": {"results": []}}
            )

        asyncio.create_task(answer())
        await FullResetPolicy().between_scenarios(channel, completed_index=0, total=2)

        assert sent[0]["type"] == MessageType.ACTION.value
        assert sent[0]["payload"]["actions"] == [
            {"id": 1, "jsonrpc": "2.0", "method": "reset_game", "params": []}
        ]

    asyncio.run(run())
