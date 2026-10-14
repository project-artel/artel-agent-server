"""이 런이 부른 `run_macro` 와 손으로 보낸 action 의 수 (ARTEL-1026).

`QaRunState.dispatches` 는 손으로 부른 action tool 만 담는다(`action_tools._recorded`).
macro 가 보내는 action 은 그 tool 을 거치지 않으므로, 어느 step 에서 `run_macro` 를
불렀는지는 지금까지 아무 데도 남지 않았다. macro hint 가 이 기록을 읽는다 — 이 step 에서
손으로 몇 번 조작했나, `run_macro` 를 이미 불렀나.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MacroCall:
    """`run_macro` 한 번. `resume_macro` 는 새 호출이 아니라 같은 호출의 이어짐이다."""

    step: int
    name: str
    # agent 가 `run_macro(arguments=...)` 로 넘긴 그대로.
    arguments: dict[str, Any]
    # 부르기 직전과 돌아온 뒤의 `scene`. 모르면 빈 문자열이다.
    scene: str
    scene_after: str
    # 끝까지 갔나(`MacroRunResult.passed`), `checkpoint` 에서 멈췄나.
    passed: bool
    paused: bool


@dataclass
class MacroCallLog:
    """`run_macro` 호출을 부른 순서대로, 그리고 step 마다 손으로 보낸 action 수."""

    calls: list[MacroCall] = field(default_factory=list)
    # step → 손으로 부른 action tool 의 수. 게임에 닿았는지와 상관없이 센다 — hint 는
    # agent 가 손으로 몇 번 시도했는지를 보는 것이지, 몇 번 성공했는지를 보지 않는다.
    hand_actions: dict[int, int] = field(default_factory=dict)

    def record_call(
        self,
        *,
        step: int,
        name: str,
        arguments: dict[str, Any],
        scene: str,
        scene_after: str,
        passed: bool,
        paused: bool,
    ) -> MacroCall:
        call = MacroCall(
            step=step,
            name=name,
            arguments=dict(arguments),
            scene=scene.strip(),
            scene_after=scene_after.strip(),
            passed=passed,
            paused=paused,
        )
        self.calls.append(call)
        return call

    def record_hand_action(self, step: int | None) -> int:
        """손 조작 하나를 세고, 그 step 에서 몇 번째인지 돌려준다. step 을 모르면 0 이다."""
        if not isinstance(step, int):
            return 0
        self.hand_actions[step] = self.hand_actions.get(step, 0) + 1
        return self.hand_actions[step]

    def calls_in_step(self, step: int) -> list[MacroCall]:
        return [call for call in self.calls if call.step == step]
