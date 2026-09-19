"""phase cycle 이 켜졌을 때만 tool 목록에 드는 둘.

둘 다 게임도 orchestration 도 안 건드린다. 하는 일은 지금까지 침묵이던 자리에 답을 만드는
것뿐이다 — `decide_next_action` 은 `DECIDE` 를, `skip_memory_update` 는 "적을 것이 없다" 를
`qa_log` 에 남는 호출로 바꾼다.

화면을 안 돌려준다. 지식 tool 들과 같은 판단이다(ARTEL-180) — 게임을 안 건드린 호출에 화면을
실으면 에이전트가 이미 들고 있는 것을 문맥에 한 번 더 사는 것이고, 델타가 "마지막 행위 이후"
라 같은 것이 두 번 실린다.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext

DECIDE_NEXT_ACTION_DESCRIPTION = """State what you are about to do for this step, before you do it.

One call, one step, and it must come before the action tool. Say the step number,
the single thing you are about to do in `plan`, and in `expected` what you will
see on screen if it worked — that sentence is what you will judge the step
against in `report_step`, and writing it after the fact is not the same thing.

This changes nothing in the game and nothing on the screen. It exists so the
decision is a call somebody can read back, rather than a sentence that was never
written down anywhere.

Keep it to the one next action. "Open the shop, buy the sword and equip it" is
three steps' worth of plan and the middle of it cannot be verified."""

SKIP_MEMORY_UPDATE_DESCRIPTION = """Say that this step left nothing worth keeping past the end of the run.

Call this instead of `record_knowledge`, `record_capability_verdict` or
`record_new_capability` when there is nothing to write. It is a real answer, not
a way past the step: most steps genuinely leave nothing behind, and a run that
goes hunting for things to write down has stopped testing.

`reason` is one line saying why there is nothing — "the button did exactly what
its label says", "this step only repeated step 4 on another row". It is required.
An empty `reason` is refused, because a skip that costs nothing to write is one
you write every step without looking."""


def build_phase_tools(ctx: ToolContext) -> list[BaseTool]:
    """이 mode 가 쓰는 phase tool. `off` 와 `in_verdict` 에서는 빈 목록이다."""
    # 아래 tool 이 closure 로 잡는 것. 되묶는 이유는 `tool_context.py` 에 있다.
    channel, state, mode = ctx.channel, ctx.state, ctx.arch.phase_cycle
    _answer = ctx.answer

    if not mode.gates_phases:
        return []

    @tool(description=SKIP_MEMORY_UPDATE_DESCRIPTION)
    async def skip_memory_update(step: int, reason: str) -> str:
        # What the agent reads is SKIP_MEMORY_UPDATE_DESCRIPTION, not this.
        #
        # `thought` 를 안 받는 유일한 tool 이다. 다른 tool 에서 `thought` 가 하는 일을
        # `reason` 이 그대로 하고, 둘을 함께 받으면 모델이 같은 문장을 두 칸에 적는다.
        why = (reason or "").strip()
        if not why:
            # 이 호출로 `UPDATE_MEMORY` 가 끝났다고 치면 안 된다. 빈 `reason` 하나로 질문을
            # 넘어갈 수 있으면 물어본 적이 없는 것과 같다.
            state.phase_cycle.hold()
            return (
                "`reason` is empty, so nothing was recorded and this step's "
                "UPDATE_MEMORY is still open. Say in one line why there is nothing "
                "worth keeping — if writing that line is not worth it, there was "
                "probably something to record after all."
            )
        return _answer(
            "Noted: nothing from this step goes to the knowledge base or the content map.",
            channel.drain_operator_messages(),
            screen=False,
        )

    if not mode.decides_in_its_own_turn:
        return [skip_memory_update]

    @tool(description=DECIDE_NEXT_ACTION_DESCRIPTION)
    async def decide_next_action(step: int, plan: str, expected: str, thought: str) -> str:
        # What the agent reads is DECIDE_NEXT_ACTION_DESCRIPTION, not this.
        #
        # 인자를 검사하지 않는다. `skip_memory_update` 의 `reason` 과 다른 점은 빈 값이
        # 무엇을 망가뜨리느냐다 — 빈 `reason` 은 "없다" 를 공짜로 만들어 답을 지우지만,
        # 빈 `plan` 은 그 자체로 `DECIDE` 가 비었다는 기록이고 그것이 이 arm 이 재려는
        # 것이다. 거절하면 재려던 값을 거절로 덮어쓴다.
        return _answer(
            f"Noted for step {step}. Now do it, then check it against what you just "
            "said you would see.",
            channel.drain_operator_messages(),
            screen=False,
        )

    return [decide_next_action, skip_memory_update]


__all__ = [
    "DECIDE_NEXT_ACTION_DESCRIPTION",
    "SKIP_MEMORY_UPDATE_DESCRIPTION",
    "build_phase_tools",
]
