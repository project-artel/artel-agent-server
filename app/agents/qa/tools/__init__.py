"""QA 에이전트가 게임을 몰기 위해 쓰는 도구.

도구 하나하나는 에이전트가 스스로 하기로 한 왕복이다. 그것이 옛 설계와 갈라지는
지점이다 — 런이 나아가는 것은 에이전트가 물었기 때문이지, 게임이 마침 무언가를 보내서가
아니다.

주제별로 나뉘어 있고, 이 파일은 그것을 한 목록으로 조립한다. 목록의 **순서가 곧 계약**
이다. `app/qa/run_config.py` 가 이 순서를 run config 에 저장하고, 모델도 이 순서로 도구를
받는다.

각 builder 의 `return` 목록에 적히는 것은 데코레이터가 붙은 도구 그 자체이지 함수가
아니다. `@tool` 이 이름을 함수에서 가져가므로, 도구가 자기 이름과 어긋난 문자열 아래
등록될 일이 없다.
"""

from langchain_core.tools import BaseTool

from app.agents.qa.arch import ResolvedArch, default_resolved_arch
from app.agents.qa.tools.action_tools import build_action_tools
from app.agents.qa.tools.capability_tools import build_capability_tools
from app.agents.qa.tools.knowledge_tools import build_knowledge_tools
from app.agents.qa.tools.observation_tools import build_capture_tool, build_observation_tools
from app.agents.qa.tools.phase import PhaseCycle, build_phase_cycle
from app.agents.qa.tools.phase_tools import build_phase_tools
from app.agents.qa.tools.reporting_tools import build_reporting_tools
from app.agents.qa.tools.screen_tools import build_screen_selector_tools
from app.agents.qa.tools.state import PendingCapture, QaRunState
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import QaRunChannel


def build_tools(
    channel: QaRunChannel, state: QaRunState, arch: ResolvedArch | None = None
) -> list[BaseTool]:
    ctx = ToolContext(channel, state, arch or default_resolved_arch())
    # 이 목록의 순서가 계약이다. `app/qa/run_config.py` 가 이 순서를 run config 의
    # `tools` 에 저장하고, 모델도 이 순서로 도구를 받는다. 주제 하나를 위아래로 옮기면
    # 저장되는 값과 모델이 보는 목록이 함께 바뀐다.
    tools: list[BaseTool] = [
        *build_observation_tools(ctx),
        *build_knowledge_tools(ctx),
        *build_screen_selector_tools(ctx),
        *build_capability_tools(ctx),
        *build_action_tools(ctx),
        *build_reporting_tools(ctx),
    ]

    # A run without vision is not offered the tool at all. Left in, it would be
    # called, cost a game round trip, and produce an image nothing can look at —
    # and the agent would have no way to know why looking did not help. This is
    # also why the tool set is part of the arch fingerprint: a run with the tool
    # and a run without it are two different agents, not one agent configured.
    if ctx.arch.vision:
        tools.append(build_capture_tool(ctx))

    # phase tool 둘은 맨 뒤다. 위 목록의 순서가 계약이라 중간에 끼우면 `off` 런과 phase 런이
    # 같은 tool 을 다른 자리에서 받고, `run_config` 의 `tools` 도 그만큼 어긋난다. 맨 뒤에
    # 붙이면 `off` 런의 목록은 한 글자도 안 바뀐다.
    tools += build_phase_tools(ctx)

    # 검사하는 자리가 여기 하나다. tool 은 `phase.py` 의 표에서 자기 phase 를 선언만 한다 —
    # tool 마다 스스로 세게 하면 tool 이 늘 때마다 하나씩 빠진다(`state.py` 의 docstring 이
    # 같은 이유를 적어 뒀다).
    #
    # `off` 와 `in_verdict` 에서는 `cycle` 이 `None` 이라 tool 이 감싸이지 않는다. 감싸는 것이
    # tool 의 이름도 `args` 도 안 바꾸므로 fingerprint 에는 안 잡히지만, 안 감싸면 실행 경로도
    # 종전 그대로라는 것이 읽는 사람에게 보인다.
    cycle = build_phase_cycle(ctx.arch.phase_cycle)
    state.phase_cycle = cycle
    if cycle is None:
        return tools
    return [_phase_gated(one, cycle) for one in tools]


def _phase_gated(tool: BaseTool, cycle: PhaseCycle) -> BaseTool:
    """자리에 안 맞으면 실행 대신 거절 문자열을 내는 같은 tool.

    이름도 설명도 `args` 도 그대로다. 바꾸는 것은 실행되는 함수 하나뿐이라 모델이 받는 tool
    선언이 안 움직이고, 그래서 prompt 접두도 안 움직인다 — 거절은 대화 끝에 붙는 tool result
    한 줄이다.

    `coroutine` 이 없는 tool 은 여기서 멈춘다. 조용히 지나가게 두면 그 tool 만 phase 검사를
    안 받고, 그것이 이 파일에 검사를 모아 둔 이유를 무효로 만든다. QA tool 은 전부 `async def`
    이므로 이 예외는 새 tool 을 동기 함수로 쓴 날에만 오른다.
    """
    inner = tool.coroutine
    if inner is None:
        raise AssertionError(
            f"tool {tool.name!r} has no coroutine, so the phase cycle cannot gate it."
        )
    name = tool.name

    async def guarded(*args, **kwargs):
        refusal = cycle.refusal_for(name)
        if refusal is not None:
            return refusal
        result = await inner(*args, **kwargs)
        # 돈 뒤에 넘긴다. 예외로 끝난 호출은 phase 를 안 옮긴다 — 실패한 조작으로 `ACT` 가
        # 끝났다고 치면 그 다음 판정이 아무것도 안 일어난 화면을 판정한다.
        cycle.advance(name)
        return result

    return tool.model_copy(update={"coroutine": guarded})


__all__ = ["PendingCapture", "QaRunState", "build_tools"]
