"""이전 TC 검증 과정과 사람이 알려준 내용을 읽는 도구."""

import logging
from uuid import UUID

from langchain_core.tools import BaseTool, tool

from app.agents.qa.tools.tool_context import ToolContext
from app.prompts import load_prompt
from app.qa.case_history import fetch_case_history, render_case_history

logger = logging.getLogger(__name__)


def build_history_tools(ctx: ToolContext) -> list[BaseTool]:
    # qa_run 구판을 고정한 실행에서도 새 도구를 조립하도록 독립 판본을 쓴다.
    description = load_prompt("qa_history", "tool_read_test_case_history", "v1").body

    @tool(description=description)
    async def read_test_case_history(
        step: int,
        thought: str,
        case_id: int,
        previous_try_id: int | None = None,
        before_try_id: int | None = None,
        after_log_id: int = 0,
        after_question_id: UUID | None = None,
        before_case_attempt_id: UUID | None = None,
    ) -> str:
        allowed = {identifier for identifier, _ in ctx.state.step_meta if identifier is not None}
        if case_id not in allowed:
            return "This case_id is not in the current scenario. Use a case_id from its TC list."
        if after_log_id < 0 or ((after_log_id > 0 or after_question_id is not None or before_case_attempt_id is not None) and previous_try_id is None):
            return "A timeline cursor requires previous_try_id and after_log_id >= 0."
        try:
            history = await fetch_case_history(
                ctx.channel.qa_try_id, case_id, previous_try_id, before_try_id, after_log_id,
                str(after_question_id) if after_question_id else None,
                str(before_case_attempt_id) if before_case_attempt_id else None,
            )
            body = render_case_history(history)
        except Exception:
            # 조회 실패를 빈 이력으로 바꾸면 기록이 없었다는 잘못된 근거가 된다.
            logger.warning("[QA] previous TC verification lookup failed", exc_info=True)
            body = (
                "Previous verification history is unavailable. This does not mean no records exist. "
                "Continue verifying the current game; you may retry this lookup later."
            )
        return ctx.answer(body, ctx.channel.drain_operator_messages(), screen=False)

    return [read_test_case_history]
