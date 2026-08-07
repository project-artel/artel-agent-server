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


class ResetPolicy(Protocol):
    """한 시나리오가 끝난 뒤, 다음 시나리오 전에 게임을 되돌리는 방법."""

    async def between_scenarios(
        self, channel: QaRunChannel, completed_index: int, total: int
    ) -> None:
        """`completed_index`(0-based) 시나리오가 끝났고 `total`개 중 다음이 남았을 때 호출된다."""
        ...


class FullResetPolicy:
    """매 시나리오 후 게임을 초기 상태로 되돌린다(reset_game).

    reset_game은 첫 씬을 리로드하고 인메모리 상태(매니저·점수·인벤)를 지운다. 그래서
    다음 시나리오의 setup은 항상 '초기 화면'에서 출발한다 — 예측 가능하지만 매번 전체
    초기화라 느리다. 최소 초기화가 필요해지면 이 정책을 다른 구현으로 교체한다(run 루프
    변경 없이).
    """

    async def between_scenarios(
        self, channel: QaRunChannel, completed_index: int, total: int
    ) -> None:
        await channel.dispatch_actions(
            [JsonRpcAction(id=1, method="reset_game")],
            "Resetting the game before the next scenario",
        )


# run 루프의 기본 정책. 주입으로 교체 가능(테스트·향후 최소-초기화 정책).
DEFAULT_RESET_POLICY: ResetPolicy = FullResetPolicy()
