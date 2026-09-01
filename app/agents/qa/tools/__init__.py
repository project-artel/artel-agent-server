"""QA 에이전트가 게임을 몰기 위해 쓰는 도구.

도구 하나하나는 에이전트가 스스로 하기로 한 왕복이다. 그것이 옛 설계와 갈라지는
지점이다 — 런이 나아가는 것은 에이전트가 물었기 때문이지, 게임이 마침 무언가를 보내서가
아니다.

주제별로 나뉘어 있고, 이 파일은 그것을 한 목록으로 조립한다. 목록의 **순서가 곧 계약**
이다. `app/qa/run_config.py` 가 이 순서를 run config 에 저장하고, 모델도 이 순서로 도구를
받는다.
"""

from langchain_core.tools import BaseTool

from app.agents.qa.arch import ResolvedArch, default_resolved_arch
from app.agents.qa.tools.action_tools import build_action_tools
from app.agents.qa.tools.knowledge_tools import build_knowledge_tools
from app.agents.qa.tools.observation_tools import build_capture_tool, build_observation_tools
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
    return tools


__all__ = ["PendingCapture", "QaRunState", "build_tools"]
