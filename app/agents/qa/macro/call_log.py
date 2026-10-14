"""이 런이 부른 `run_macro` 와 손으로 보낸 action 의 수 (ARTEL-1026).

`QaRunState.dispatches` 는 손으로 부른 action tool 만 담는다(`action_tools._recorded`).
macro 가 보내는 action 은 그 tool 을 거치지 않으므로, `run_macro` 를 몇 번 어떤 `scene` 에서
불렀는지는 지금까지 아무 데도 남지 않았다. 이 기록을 세 곳이 읽는다.

- stage draft: 한 `scene` visit 동안 부른 macro 를 엮어 초안을 만든다.
- macro hint: 이 step 에서 `run_macro` 를 이미 불렀으면 손 조작에 hint 를 안 붙인다.
- macro result `fold`: 결과를 낸 호출이 어느 step 의 것이었나 본다.

**visit** 은 같은 `scene` 이름이 이어지는 구간이다. 호출 하나의 `scene` 이 바로 앞 호출의
`scene_after` 와 다르면 새 visit 이 시작된다. 같은 `scene` 에 나갔다가 돌아오면 그것도 새
visit 이다 — 그 사이 다른 `scene` 에서 부른 호출이나 손 조작이 끼기 때문이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MacroCall:
    """`run_macro` 한 번. `resume_macro` 는 새 호출이 아니라 같은 호출의 이어짐이다."""

    step: int
    name: str
    # agent 가 `run_macro(arguments=...)` 로 넘긴 그대로. 초안이 같은 값을 싣는다.
    arguments: dict[str, Any]
    # 부르기 직전과 돌아온 뒤의 `scene`. 모르면 빈 문자열이다.
    scene: str
    scene_after: str
    # 끝까지 갔나(`MacroRunResult.passed`), `checkpoint` 에서 멈췄나.
    passed: bool
    paused: bool
    visit: int


@dataclass
class MacroCallLog:
    """`run_macro` 호출을 부른 순서대로, 그리고 step 마다 손으로 보낸 action 수."""

    calls: list[MacroCall] = field(default_factory=list)
    # step → 손으로 부른 action tool 의 수. 게임에 닿았는지와 상관없이 센다 — hint 는
    # agent 가 손으로 몇 번 시도했는지를 보는 것이지, 몇 번 성공했는지를 보지 않는다.
    hand_actions: dict[int, int] = field(default_factory=dict)
    # 지금 visit 의 번호와 그 visit 의 `scene`. 손 조작도 `scene` 을 바꿀 수 있어 둘 다
    # `notice_scene` 으로 알린다.
    _visit: int = 0
    _visit_scene: str = ""

    def notice_scene(self, scene: str) -> int:
        """지금 서 있는 `scene` 을 알리고, 그 `scene` 의 visit 번호를 돌려준다.

        빈 문자열은 모르는 것이라 visit 을 바꾸지 않는다.
        """
        scene = scene.strip()
        if scene and scene != self._visit_scene:
            self._visit += 1
            self._visit_scene = scene
        return self._visit

    @property
    def visit(self) -> int:
        return self._visit

    @property
    def visit_scene(self) -> str:
        return self._visit_scene

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
        visit = self.notice_scene(scene)
        call = MacroCall(
            step=step,
            name=name,
            arguments=dict(arguments),
            scene=scene.strip(),
            scene_after=scene_after.strip(),
            passed=passed,
            paused=paused,
            visit=visit,
        )
        self.calls.append(call)
        self.notice_scene(scene_after)
        return call

    def record_hand_action(self, step: int | None, scene: str, scene_after: str) -> int:
        """손 조작 하나를 세고, 그 step 에서 몇 번째인지 돌려준다. step 을 모르면 0 이다."""
        self.notice_scene(scene)
        self.notice_scene(scene_after)
        if not isinstance(step, int):
            return 0
        self.hand_actions[step] = self.hand_actions.get(step, 0) + 1
        return self.hand_actions[step]

    def calls_in_step(self, step: int) -> list[MacroCall]:
        return [call for call in self.calls if call.step == step]

    def calls_in_visit(self, visit: int) -> list[MacroCall]:
        return [call for call in self.calls if call.visit == visit]
