"""시나리오 사이 게임 상태 초기화 정책 (seam) — ARTEL-258.

TR 단위 QA 실행에서 run 루프는 "무엇을 얼마나 되돌리나"를 직접 정하지 않고 이 정책에
위임한다. 지금은 매 시나리오 후 전체 초기화(`FullResetPolicy`)만 있지만, 나중에 초기화를
**최소화하는 정책**(예: 다음 시나리오의 사전조건이 현재 상태에서 이미 만족되면 안 되돌림)
으로 갈아끼울 수 있도록 이 자리를 둔다.

정책이 곧 "다음 시나리오의 setup이 어떤 시작 상태에서 출발하는가"를 정의한다. 지금은
초기 화면(reset_game이 첫 씬을 리로드)이지만, 이 가정은 정책에 묶여 있을 뿐 루프에
하드코딩돼 있지 않다.

"""

from typing import Protocol

from app.qa.channel import QaRunChannel
from app.qa.envelope import JsonRpcAction


def reset_game_cheat() -> JsonRpcAction:
    return JsonRpcAction(
        id=1,
        method="invoke_cheat",
        params=[{"name": "reset_game"}],
    )


class ResetPolicy(Protocol):
    """한 시나리오가 끝난 뒤, 다음 시나리오 전에 게임을 되돌리는 방법."""

    async def between_scenarios(
        self, channel: QaRunChannel, completed_index: int, total: int
    ) -> None:
        """`completed_index`(0-based) 시나리오가 끝났고 `total`개 중 다음이 남았을 때 호출된다."""
        ...


class FullResetPolicy:
    """다음 시나리오가 시작되기 전에 SDK의 reset_game 치트를 호출한다."""

    async def between_scenarios(
        self, channel: QaRunChannel, completed_index: int, total: int
    ) -> None:
        await channel.dispatch_actions(
            [reset_game_cheat()],
            "Resetting the game before the next scenario",
        )


# run 루프의 기본 정책. 주입으로 교체 가능(테스트·향후 최소-초기화 정책).
DEFAULT_RESET_POLICY: ResetPolicy = FullResetPolicy()
