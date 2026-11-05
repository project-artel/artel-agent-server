"""`checkpoint` 에서 멈췄다가 이어 도는 macro 호출 하나.

**macro 의 상태는 이 coroutine 안에 산다.** 묶인 이름, 호출 사슬, 지금 위치, 반복의 회,
짝 카운터가 전부 runner 의 지역 변수와 재귀 호출에 들어 있다. 그것을 다른 모양으로
옮겨 적고 다시 세우는 대신, runner 를 task 하나로 돌리고 `checkpoint` 에서 그 task 를
**멈춰 세운다.** 이어 돌면 처음이 아니라 그 줄부터 도는 것이 이것으로 공짜다.

**비동기로 함께 도는 것이 아니다.** ARTEL-949 가 금지한 것은 macro 와 agent 가 동시에
게임을 모는 것이다 — SDK 는 batch 끼리 섞으므로 어느 쪽 클릭이 먼저 갈지 모르게 된다.
여기서 task 는 두 경우에만 움직인다. tool 호출이 그것을 기다리는 동안, 그리고 그 사이에
멈춰 선 task 는 아무것도 안 보낸다. 게임을 모는 쪽은 언제나 하나다.

**task 는 그것을 기다리는 쪽과 함께 끝난다.** tool 호출이 취소되면(deadline, operator 의
`STOP`) task 도 끊는다. 안 끊으면 런이 끝난 뒤에도 macro 가 혼자 action 을 보낸다 —
`asyncio.wait` 는 기다리던 쪽이 취소돼도 task 를 건드리지 않는다.

**멈춘 것을 버려도 게임에 남는 것이 없다.** parser 가 누름과 뗌 사이의 `checkpoint` 를
저장 때 거절하므로(`_CallGraph.reject_checkpoint_while_holding`), 멈춘 macro 는 눌러 둔
키도 멈춘 시간도 들고 있지 않다. 그래서 런이 끝날 때 task 를 끊기만 하면 된다.
"""

import asyncio
from collections.abc import Coroutine
from typing import Any

from app.agents.qa.macro.runner import MacroRunResult


class MacroSession:
    """runner task 하나와, `checkpoint` 에서 agent 의 답을 기다리는 자리."""

    def __init__(self, name: str, step: int) -> None:
        self.name = name
        # 이 macro 를 부른 step. 이어 돌 때도 action 은 이 step 에 적힌다 — macro 하나가
        # 시나리오 step 하나에 속한다.
        self.step = step
        self.task: asyncio.Task[MacroRunResult] | None = None
        self._paused = asyncio.Event()
        self._decision: asyncio.Future[bool] | None = None
        self._result: MacroRunResult | None = None

    def start(self, run: Coroutine[Any, Any, MacroRunResult]) -> None:
        self.task = asyncio.create_task(run)

    async def pause(self, result: MacroRunResult) -> bool:
        """runner 쪽에서 부른다. agent 가 `resume_macro` 로 답할 때까지 돌아오지 않는다."""
        self._result = result
        self._decision = asyncio.get_running_loop().create_future()
        self._paused.set()
        return await self._decision

    async def settle(self) -> tuple[MacroRunResult, bool]:
        """macro 가 멈춰 서거나 끝날 때까지 기다린다. `(결과, 멈췄나)` 를 낸다.

        끝났으면 task 의 결과를 그대로 낸다 — task 가 예외로 끝났으면 그 예외가 여기서
        다시 오른다. `QaCancelled` 가 그 경우이고, 부르는 쪽이 종전처럼 그것을 통과시킨다.
        """
        assert self.task is not None
        waiter = asyncio.create_task(self._paused.wait())
        try:
            done, _pending = await asyncio.wait(
                {self.task, waiter}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            waiter.cancel()
            self.cancel()
            raise
        if self.task in done:
            waiter.cancel()
            return self.task.result(), False
        self._paused.clear()
        assert self._result is not None
        return self._result, True

    def resume(self, proceed: bool) -> None:
        """멈춘 자리에서 잇거나(`True`) 그만둔다(`False`). 그만두면 더 아무것도 안 보낸다."""
        assert self._decision is not None and not self._decision.done()
        self._decision.set_result(proceed)

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()
