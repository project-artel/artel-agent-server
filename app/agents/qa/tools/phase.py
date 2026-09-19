"""런이 지금 `OBSERVE -> DECIDE -> ACT -> VERIFY -> UPDATE_MEMORY` 중 어디에 있나.

`PhaseCycleMode.lite` 와 `full` 에서만 산다. `off` 와 `in_verdict` 는 이 파일을 아예 만들지
않는다 — `build_phase_cycle` 이 `None` 을 돌려주고, tool 은 종전대로 언제나 실행된다.

## 전이는 어느 tool 이 불렸나로만 정한다

모델에게 "이 전환이 의미 있나" 를 묻지 않는다. 물으면 재려는 축이 둘이 된다 — phase 가
도움이 되는가와 모델이 phase 를 옳게 고르는가. 아래 `_TOOL_PHASE` 표가 tool 이름 하나를
phase 하나에 붙이고, 런은 **더 뒤 phase 의 tool 을 불러서** 앞으로 간다. `VERIFY` 와
`DECIDE` 는 자기 tool 로 끝나지만 `OBSERVE`·`ACT`·`UPDATE_MEMORY` 는 안 끝난다 — 한 step 이
보는 것도 누르는 것도 적는 것도 한 번으로 안 끝나기 때문이다(`_REPEATABLE`).

## 거절은 tool result 한 줄이다

자리에 안 맞는 호출은 실행되지 않고 문자열 하나로 답한다. tool 목록도, 이미 보낸 메시지도
고치지 않는다. `ModelRequest.override(tools=...)` 로 매 호출 목록을 phase 것만 남기는 길이
있지만 tool 선언이 요청 맨 앞에 있어서 prompt 접두가 매 턴 달라지고, 그 접두를 더 약하게
깨뜨린 것만으로 cache 적중이 97.3% 에서 1.3% 로, 런 하나 비용이 $0.87 에서 $14.22 로 간
실측이 있다 (ARTEL-868).

## 세는 자리가 하나다

tool 은 `_TOOL_PHASE` 에서 자기 phase 를 선언만 하고, 검사는 `app/agents/qa/tools/__init__.py`
의 wrapper 한 자리에서 한다. `state.py` 의 docstring 이 적어 둔 것과 같은 이유다 — 세는 자리가
tool 마다 흩어지면 tool 이 늘 때마다 하나씩 빠진다.
"""

from enum import StrEnum

from app.agents.qa.arch import PhaseCycleMode


class RunPhase(StrEnum):
    """다섯 단계. 값이 `qa_log` 와 거절 문구에 그대로 실린다."""

    observe = "OBSERVE"
    decide = "DECIDE"
    act = "ACT"
    verify = "VERIFY"
    update_memory = "UPDATE_MEMORY"


# phase 와 무관하게 언제나 통과하는 tool. **이 목록이 없으면 런이 갇힌다.**
#
# - `observe_scene` — 화면은 언제든 다시 봐야 한다. 로딩·애니메이션·카운트다운이 끝났는지는
#   다시 보는 것 말고 알 길이 없다.
# - `reply_to_operator`, `wait_for_operator` — 사람이 끼어드는 자리에 phase 를 걸 수 없다.
# - `report_issue` — 게임이 깨진 것을 보는 순간이 언제일지 이쪽이 정할 수 없다.
# - `finish_run` — 종료를 막으면 런이 안 끝난다.
# - `compact_context` — 압축은 phase 밖의 일이다.
# - `pause_game_time`, `resume_game_time` — 판정하려고 화면을 세우는 것은 관측이지 조작이
#   아니다. 그래서 `ACT` 를 끝내지도 않는다(아래 표에 없다).
# - `include_screen_selector`, `exclude_screen_selector` — 관측 설정이다.
ALWAYS_ALLOWED = frozenset(
    {
        "observe_scene",
        "reply_to_operator",
        "wait_for_operator",
        "report_issue",
        "finish_run",
        "compact_context",
        "pause_game_time",
        "resume_game_time",
        "include_screen_selector",
        "exclude_screen_selector",
    }
)


# tool 이름 -> 그 tool 이 사는 phase. 여기 없는 이름은 검사를 지나간다.
#
# 지나가게 두는 이유: 표에 빠진 tool 하나 때문에 런이 영영 갇히는 쪽이, 새 tool 이 한동안
# 어느 phase 에도 안 묶이는 쪽보다 나쁘다. 빠뜨리는 것은 테스트가 잡는다 —
# `tests/test_qa_phase_cycle.py` 가 기본 tool 목록의 모든 이름이 이 표나 `ALWAYS_ALLOWED`
# 중 하나에 있는지 본다.
_TOOL_PHASE: dict[str, RunPhase] = {
    # OBSERVE — 화면과 이미 알려진 것을 읽는 자리.
    "observe_scene": RunPhase.observe,
    "inspect_object": RunPhase.observe,
    "capture_screen": RunPhase.observe,
    "list_scene_capabilities": RunPhase.observe,
    "search_knowledge": RunPhase.observe,
    "expand_knowledge": RunPhase.observe,
    # DECIDE — `full` 에만 있는 tool 하나.
    "decide_next_action": RunPhase.decide,
    # ACT — `action_tools.py` 의 16 개 중 게임을 실제로 움직이는 14 개.
    # `pause_game_time` 과 `resume_game_time` 은 위 `ALWAYS_ALLOWED` 로 갔다.
    "click_button": RunPhase.act,
    "enter_text": RunPhase.act,
    "press_key": RunPhase.act,
    "move_pointer": RunPhase.act,
    "click": RunPhase.act,
    "double_click": RunPhase.act,
    "hold_mouse_button": RunPhase.act,
    "release_mouse_button": RunPhase.act,
    "hold_key": RunPhase.act,
    "release_key": RunPhase.act,
    "set_input_axis": RunPhase.act,
    "set_input_button": RunPhase.act,
    "drag": RunPhase.act,
    "reset_game": RunPhase.act,
    # VERIFY — 판정 하나.
    "report_step": RunPhase.verify,
    # UPDATE_MEMORY — 런을 넘어 남는 것을 적는 자리, 그리고 적을 것이 없다고 답하는 자리.
    "record_capability_verdict": RunPhase.update_memory,
    "record_new_capability": RunPhase.update_memory,
    "record_knowledge": RunPhase.update_memory,
    "update_knowledge": RunPhase.update_memory,
    "link_knowledge": RunPhase.update_memory,
    "forget_knowledge": RunPhase.update_memory,
    "unlink_knowledge": RunPhase.update_memory,
    "skip_memory_update": RunPhase.update_memory,
}


_LITE_ORDER = (RunPhase.observe, RunPhase.act, RunPhase.verify, RunPhase.update_memory)
_FULL_ORDER = (
    RunPhase.observe,
    RunPhase.decide,
    RunPhase.act,
    RunPhase.verify,
    RunPhase.update_memory,
)


# 같은 phase 에서 연속으로 몇 번까지 거절한 뒤 통과시키나.
#
# 2 로 둔 이유는 왕복 산수다. 거절 하나는 모델 호출 하나를 쓰고 게임도 상태도 안 움직인다.
# 첫 거절은 값이 있다 — 모델이 이 phase 가 무엇을 기다리는지 그때 처음 듣는다. 둘째까지도
# 같은 tool 을 부르면 문구를 못 읽은 것이고, 셋째 거절이 그것을 바꿀 근거가 없다. 그러니 한
# 스텝에서 낭비하는 왕복 상한이 phase 당 2, 다섯 단계면 10 이다. 3 으로 두면 15 가 되는데,
# 얻는 것은 "한 번 더 물어보면 맞힐지도 모른다" 뿐이다.
#
# 통과시킬 때 그냥 지나가게 두지 않고 **모델이 부른 tool 의 phase 로 옮긴다.** 모델이 두 번
# 연속으로 같은 곳을 가리켰으면 이쪽 표가 아니라 모델이 런의 실제 자리를 알고 있는 것이고,
# 통과만 시키고 phase 를 두면 다음 호출이 또 거절당한다.
#
# 통과한 횟수는 `forced_passes` 로 남는다. 거절 수가 높은 채로 끝난 파일럿은 "phase 가 안
# 돕는다" 가 아니라 "모델이 phase 를 안 따랐고 왕복만 태웠다" 이고, 그 둘을 못 가르면 파일럿이
# 답을 못 낸다.
MAX_CONSECUTIVE_REFUSALS = 2


# 자기 tool 이 돌아도 phase 가 안 넘어가는 자리. **셋이다 — `OBSERVE`, `ACT`, `UPDATE_MEMORY`.**
#
# 한 시나리오 step 이 tool 호출 하나라는 가정이 세 자리에서 다 틀렸다.
#
# `ACT` — L1 의 step 3 은 "오프닝 스토리를 끝까지 진행한다" 하나이고, 그러려면 키를 여러 번
# 눌러야 한다. 2026-09-18 파일럿에서 거절 147 회 중 51 회가 `VERIFY` 에서 `press_key` 를
# 부른 것이었다.
#
# `OBSERVE` — 읽는 일도 한 번에 안 끝난다. `search_knowledge` 로 찾은 것을
# `expand_knowledge` 로 펴 보고, `capture_screen` 을 다시 찍고, `inspect_object` 로 하나를
# 더 들여다보는 것이 한 자리에서 일어난다. 첫 호출에 phase 를 넘기면 둘째부터 거절이다.
#
# `UPDATE_MEMORY` — 한 스텝이 남기는 것이 하나가 아니다. 틀린 항목을 `forget_knowledge` 로
# 지우고 `record_knowledge` 로 다시 적는 것이 한 번의 정정이고, capability 판정과 지식을
# 같이 남기는 스텝도 있다.
#
# 반복 phase 를 끝내는 것은 자기 tool 이 아니라 **다음으로 가는 tool** 이다. `report_step`
# 이 `ACT` 와 `VERIFY` 를 함께 닫는 것이 그 예다 — 판정하는 행위가 곧 "그만 조작한다" 는
# 선언이기 때문이다.
_REPEATABLE = frozenset({RunPhase.observe, RunPhase.act, RunPhase.update_memory})

# 안 들러도 되는 phase. **`UPDATE_MEMORY` 는 여기 없다** — 이 축이 존재하는 이유가 그것이고
# 나머지는 그 둘레의 비계다.
#
# `OBSERVE` 가 여기 있는 것은 prompt 가 이미 참이라고 가르치는 것을 gate 가 몰랐기 때문이다.
# `system.md` 는 action tool 이 "returns the outcome AND the scene it produced" 이므로
# "you usually do not need a separate observation afterwards" 라고 적는다. 그 말은 맞다 —
# 그런데 gate 가 매 바퀴 `observe_scene` 을 요구했다. 2026-09-18 ACT 수정 후 파일럿에서
# 거절 137 회 중 **97 회가 `OBSERVE` 에서** 났고, 그중 47 회는 `report_step` 이었다.
#
# `ACT` 도 건너뛸 수 있다. 관측만 하는 스텝이 있다 — L1 의 step 1 이 "타이틀 화면을
# 관찰한다" 이고, 거기서 조작을 요구하면 없는 동작을 지어내게 된다.
#
# `DECIDE` 는 여기 없다. `full` arm 의 전부가 그 phase 이고, 건너뛸 수 있으면 그 arm 이
# `lite` 와 같아진다.
_SKIPPABLE = frozenset({RunPhase.observe, RunPhase.act})

# 반복이면서 건너뛸 수는 없는 자리가 `UPDATE_MEMORY` 하나뿐이라는 것이, 이 파일이 `_satisfied`
# 를 세는 이유다. 반복만으로 두면 자기 tool 이 phase 를 안 닫으니 아무것도 안 적고 다음
# 바퀴로 넘어갈 수 있고, 그러면 gate 가 지키는 것이 없어진다. 그래서 떠나는 조건이 둘로
# 갈린다 — 건너뛸 수 있는 phase 는 언제든, 아닌 phase 는 자기 tool 이 한 번은 돈 뒤에.
assert not (_SKIPPABLE & {RunPhase.update_memory})


def _tools_that_end(phase: RunPhase) -> str:
    """그 phase 를 끝내는 tool 이름을, 거절 문구에 넣을 한 줄로."""
    names = sorted(name for name, at in _TOOL_PHASE.items() if at is phase)
    return ", ".join(f"`{name}`" for name in names)


class PhaseCycle:
    """한 런의 phase 와 거절 수. `lite`·`full` 에서만 만들어진다.

    순수 상태다 — 게임도 채널도 안 건드리고, 아는 것은 tool 이름뿐이다. 단위 테스트가 전부
    덮는 이유이자, 파일럿을 기다리는 동안 미리 지을 수 있었던 이유다.
    """

    def __init__(
        self,
        order: tuple[RunPhase, ...],
        max_consecutive_refusals: int = MAX_CONSECUTIVE_REFUSALS,
    ) -> None:
        self._order = order
        self._max_consecutive_refusals = max_consecutive_refusals
        self.phase = order[0]
        # 이 런이 자리에 안 맞는 호출을 몇 번 돌려보냈나. 파일럿의 진단 항목이다.
        self.refusals = 0
        # 상한에 닿아 그냥 통과시킨 횟수.
        self.forced_passes = 0
        self._consecutive_refusals = 0
        self._held = False
        # 지금 phase 의 tool 이 이 바퀴에서 한 번이라도 돌았나. 반복 phase 를 떠나도 되는지가
        # 여기 달렸다 — `_may_leave` 를 보라.
        self._satisfied = False

    def _after(self, phase: RunPhase) -> RunPhase:
        return self._order[(self._order.index(phase) + 1) % len(self._order)]

    def _enter(self, phase: RunPhase) -> None:
        """phase 를 옮기고, 그 자리의 일이 아직 안 됐다고 표시한다.

        고리이므로 옮긴 결과가 지금과 같은 이름일 수 있다 — `UPDATE_MEMORY` 에 앉은 채
        다음 스텝의 `report_step` 을 부르면 한 바퀴 돌아 다시 `UPDATE_MEMORY` 다. 그때도
        `_satisfied` 는 풀려야 한다. 안 그러면 첫 스텝에 적어 둔 것 하나로 남은 스텝이
        전부 기록 없이 지나간다.
        """
        self.phase = phase
        self._satisfied = False

    def _ahead(self, at: RunPhase) -> int:
        """지금 자리에서 저기까지 몇 칸 앞인가. 고리이므로 뒤란 없다."""
        here = self._order.index(self.phase)
        return (self._order.index(at) - here) % len(self._order)

    def _may_leave(self) -> bool:
        """지금 phase 를 떠날 수 있나 — 건너뛸 수 있거나, 자기 tool 이 한 번은 돌았거나."""
        return self.phase in _SKIPPABLE or self._satisfied

    def _blocking_phase(self, at: RunPhase) -> RunPhase | None:
        """저기로 못 가게 막고 있는 phase, 갈 수 있으면 `None`.

        둘 중 하나다 — 지금 앉은 자리가 아직 할 일을 안 했거나, 가는 길에 건너뛸 수 없는
        phase 가 끼어 있거나. 거절 문구가 이름을 대는 자리가 여기다.
        """
        if not self._may_leave():
            return self.phase
        here = self._order.index(self.phase)
        for step in range(1, self._ahead(at)):
            between = self._order[(here + step) % len(self._order)]
            if between not in _SKIPPABLE:
                return between
        return None

    def _reachable(self, at: RunPhase | None) -> bool:
        """지금 자리에서 저 phase 로 바로 갈 수 있나."""
        if at is None or at is self.phase:
            return False
        return self._blocking_phase(at) is None

    def hold(self) -> None:
        """이 호출은 자기 phase 를 끝낸 것으로 치지 말라고 tool 이 말하는 자리.

        gate 는 tool 이 **돌았는지**만 안다. 안에서 무엇을 답했는지는 못 본다. 그 차이가
        구멍을 하나 낸다 — `skip_memory_update` 를 빈 `reason` 으로 부르면 tool 은 거절하는데
        gate 는 `UPDATE_MEMORY` 가 끝난 것으로 읽고, 물어보려던 질문을 빈 인자 하나로 넘어갈
        수 있게 된다. `report_step` 에서 `learned` 를 빠뜨린 경우도 같다.

        그래서 그 두 자리만 이것을 부른다. **인자가 비어서 질문 자체가 답을 안 받은 경우**가
        기준이다. 지도나 지식 쓰기가 인자 모양 때문에 거절당한 것은 여기 안 든다 — 그쪽은
        모델이 적으려고는 한 것이고, 거기까지 넓히면 거절 문구가 달린 모든 갈래가 phase 를
        아는 자리가 되어 `phase.py` 에 검사를 모아 둔 뜻이 없어진다.
        """
        self._held = True

    def refusal_for(self, tool_name: str) -> str | None:
        """이 tool 을 지금 부르면 안 되는 이유, 부를 수 있으면 `None`.

        `None` 이 아닌 값을 돌려준 호출은 **실행되지 않는다.** 문자열 하나가 tool result 로
        가고 런은 그대로 간다 — 거절로 런을 실패시키지 않는다.
        """
        if tool_name in ALWAYS_ALLOWED:
            # 거절 연속 카운터를 건드리지 않는다. 이 tool 들은 어느 phase 에서 불려도 옳으
            # 므로, 사이에 하나 끼었다고 모델이 자리를 고친 것은 아니다.
            return None

        at = _TOOL_PHASE.get(tool_name)
        if at is None or at is self.phase or self._reachable(at):
            self._consecutive_refusals = 0
            return None

        if self._consecutive_refusals >= self._max_consecutive_refusals:
            self.forced_passes += 1
            self._consecutive_refusals = 0
            self._enter(at)
            return None

        self._consecutive_refusals += 1
        self.refusals += 1
        # 막고 있는 자리를 문구가 직접 댄다. 지금 앉은 phase 일 때도 있고, 가는 길에 낀 다른
        # phase 일 때도 있다 — `OBSERVE` 에서 지식을 적으려 하면 막는 것은 사이의 `VERIFY`
        # 다. 앉은 자리만 대면 그 경우에 시키는 대로 해도 또 거절당한다.
        blocking = self._blocking_phase(at) or self.phase
        return (
            f"Not now — this run is in the {self.phase.value} phase and `{tool_name}` "
            f"belongs to {at.value}. Nothing ran and nothing was recorded. "
            f"{blocking.value} ends when you call one of: "
            f"{_tools_that_end(blocking)}. Do that first; `{tool_name}` will be "
            "waiting on the other side of it."
        )

    def advance(self, tool_name: str) -> None:
        """실행된 tool 하나를 반영한다.

        `ACT` 한가운데서 `observe_scene` 을 불러도 `OBSERVE` 로 돌아가지 않는다. 뒤로 가는
        전이가 아예 없기 때문이다 — `_ahead` 가 고리를 한 방향으로만 세고, `OBSERVE` 는
        `ACT` 에서 세 칸 앞이라 사이에 낀 `VERIFY` 가 막는다.
        """
        if self._held:
            self._held = False
            return
        at = _TOOL_PHASE.get(tool_name)
        if at is self.phase:
            # 반복 phase 는 자기 tool 로 안 끝난다. 같은 step 안에서 몇 번이고 더 부를 수
            # 있고, 돈 것은 `_satisfied` 로만 남는다 — `UPDATE_MEMORY` 를 떠나도 되는지가
            # 그 한 칸에 달렸다.
            if self.phase in _REPEATABLE:
                self._satisfied = True
                return
            self._enter(self._after(self.phase))
            return
        if self._reachable(at):
            # `report_step` 이 `ACT` 와 `VERIFY` 를 함께 닫는다 — 판정이 곧 조작을 그만둔다는
            # 선언이고, 그 호출 자체가 `VERIFY` 가 요구하는 바로 그것이다. 건너뛰어 온 경우도
            # 같다: 지나온 phase 는 건너뛸 수 있는 것뿐이었고, 이 tool 이 자기 phase 를 닫는다.
            #
            # 반복 phase 로 들어갈 때는 그 자리에 앉는다. 자기 tool 이 안 닫는 자리이므로,
            # 이 한 호출로 끝났다고 치고 넘기면 두 번째 호출이 거절당한다.
            self._enter(at if at in _REPEATABLE else self._after(at))
            if at in _REPEATABLE:
                self._satisfied = True


def build_phase_cycle(mode: PhaseCycleMode) -> PhaseCycle | None:
    """이 mode 가 phase 를 강제하면 그 상태 기계, 아니면 `None`."""
    if not mode.gates_phases:
        return None
    return PhaseCycle(_FULL_ORDER if mode.decides_in_its_own_turn else _LITE_ORDER)
