"""지식 도구 일곱 개를 지금까지의 순서 그대로 낸다.

읽기와 쓰기를 다른 모듈에 두면서도 순서를 지키려면 합치는 자리가 하나 필요하다. 그
순서는 `app/qa/run_config.py` 가 run config 에 저장하고 모델도 그대로 받으므로, 바꾸면
저장되는 값과 모델이 보는 목록이 함께 바뀐다.
"""

from langchain_core.tools import BaseTool

from app.agents.qa.tools.knowledge_read_tools import build_knowledge_read_tools
from app.agents.qa.tools.knowledge_write_tools import build_knowledge_write_tools
from app.agents.qa.tools.tool_context import ToolContext


def build_knowledge_tools(ctx: ToolContext) -> list[BaseTool]:
    search_knowledge, expand_knowledge = build_knowledge_read_tools(ctx)
    return [search_knowledge, *build_knowledge_write_tools(ctx), expand_knowledge]
