"""지식창고에서 읽기만 하는 도구 둘.

둘 다 화면을 바꾸지 않는다. 그래서 결과에 화면을 얹지 않는다 — 검색을 두 번 하면 두 번째가
첫 번째와 똑같은 화면을 반복하게 되고, 그것이 문맥을 다시 쓰는 일이다(ARTEL-180).

문구와 렌더 함수는 `app/agents/qa/knowledge.py` 가 들고 있다.
"""

from langchain_core.tools import BaseTool, tool

from app.agents.qa.knowledge import (
    EXPAND_KNOWLEDGE_DESCRIPTION,
    KNOWLEDGE_RELATIONS,
    KNOWLEDGE_TAGS,
    MAX_EXPAND_DEPTH,
    RESULT_LIMIT,
    SEARCH_KNOWLEDGE_DESCRIPTION,
    SIMILAR_LABEL,
    render_expansion,
    render_results,
)
from app.agents.qa.tools.tool_context import ToolContext
from app.qa.channel import KnowledgeRequestFailed, with_operator_messages


def build_knowledge_read_tools(ctx: ToolContext) -> list[BaseTool]:
    # ctx 가 든 것을 여기서 되묶는다. 아래 tool 은 `build_tools` 한 함수 안에 있던 것을
    # 그대로 옮긴 것이라, 이 줄이 있어야 본문이 한 글자도 바뀌지 않는다. 읽는 쪽에는
    # 아래 tool 이 무엇을 closure 로 잡는지 먼저 말해 주는 머리말이기도 하다.
    channel, state, arch = ctx.channel, ctx.state, ctx.arch

    @tool(
        description=SEARCH_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_searches_per_run, tags=", ".join(KNOWLEDGE_TAGS)
        )
    )
    async def search_knowledge(
        step: int, thought: str, query: str, tag: str | None = None
    ) -> str:
        # What the agent reads is SEARCH_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Deliberately not routed through `_run`: that path dispatches actions and
        # appends the scene they produced. A search moves nothing on screen, so a
        # scene view here would be the same picture the agent already has, paid for
        # again in context — exactly what `app/agents/qa/context.py` exists to stop.
        if state.knowledge_searches_attempted >= arch.max_searches_per_run:
            return (
                f"You have used all {arch.max_searches_per_run} knowledge searches for "
                "this run. Judge the remaining steps from the scenario and what you "
                "can see."
            )

        topic = (tag or "").strip().upper()
        if topic and topic not in KNOWLEDGE_TAGS:
            # Refused before it goes out. Orchestration rejects an unknown tag
            # outright rather than ignoring it, so sending one would spend a round
            # trip and a search out of the run's budget to be told something this
            # side already knew.
            return (
                f"{tag!r} is not a knowledge topic, so the search was not sent. "
                f"Use one of {', '.join(KNOWLEDGE_TAGS)}, or leave `tag` out."
            )

        state.knowledge_searches_attempted += 1
        answer = await channel.search_knowledge(query, topic or None, RESULT_LIMIT, step)
        messages = channel.drain_operator_messages()
        remaining = arch.max_searches_per_run - state.knowledge_searches_attempted

        if answer is None:
            return with_operator_messages(
                "The knowledge base did not answer in time. Judge this step from "
                f"the scenario and what you can see. {remaining} search(es) left.",
                messages,
            )
        if isinstance(answer, KnowledgeRequestFailed):
            # Said plainly, with what to do instead. A failed lookup is a side
            # errand that failed, not a failed step — an agent told only that
            # something went wrong has been known to fail the step over it.
            return with_operator_messages(
                f"The knowledge search could not run — {answer.reason}. This says "
                "nothing about the game; judge this step from the scenario and what "
                f"you can see. {remaining} search(es) left.",
                messages,
            )
        # Remembered before the result is rendered, because this is what makes an
        # entry writable at all: `update_knowledge` and `forget_knowledge` both
        # refuse an id that never came back from a search in this run. An id-less
        # hit is skipped rather than stored under the empty string — it is not
        # addressable, so it can be neither corrected nor deleted.
        for entry in answer.results:
            if entry.id:
                state.knowledge_seen[entry.id] = entry.summary
            # Neighbours are recorded SEPARATELY, and the difference is the point.
            # `knowledge_seen` means "read in full", and that is the precondition
            # `update_knowledge` and `forget_knowledge` rest on; a clipped one-line
            # summary is not having read something. Putting neighbours in here
            # would let a run delete an entry it has only glimpsed, which is the
            # first regression this feature could ship.
            state.remember_glimpsed(entry.neighbors)
        return with_operator_messages(render_results(answer, remaining), messages)

    @tool(
        description=EXPAND_KNOWLEDGE_DESCRIPTION.format(
            limit=arch.max_expands_per_run,
            relations=", ".join(KNOWLEDGE_RELATIONS),
            similar=SIMILAR_LABEL,
        )
    )
    async def expand_knowledge(
        step: int, thought: str, knowledge_id: str, depth: int = 1
    ) -> str:
        # What the agent reads is EXPAND_KNOWLEDGE_DESCRIPTION, not this.
        #
        # Three outcomes handled exactly as `search_knowledge` handles them, and
        # for the same reason: none of timeout, refusal or empty answer is a reason
        # to fail the step.
        if state.knowledge_expands_attempted >= arch.max_expands_per_run:
            return (
                f"You have used all {arch.max_expands_per_run} knowledge expansions "
                "for this run. The neighbours already printed with your searches are "
                "what you have."
            )

        target = (knowledge_id or "").strip()
        if not state.knows_of(target):
            return (
                f"{target!r} is not an entry this run has been shown, so nothing was "
                "expanded. Search for it first and use the id printed with the hit."
            )

        state.knowledge_expands_attempted += 1
        # Clamped here as well as on the far side. Orchestration clamps rather than
        # refuses, so this only saves a round trip — but it also keeps the number
        # the agent is told about and the number it gets from drifting apart.
        answer = await channel.expand_knowledge(
            target, max(1, min(depth, MAX_EXPAND_DEPTH)), include_similar=True, step=step
        )
        messages = channel.drain_operator_messages()
        remaining = arch.max_expands_per_run - state.knowledge_expands_attempted

        if answer is None:
            return with_operator_messages(
                "The knowledge base did not answer in time. Carry on with what you "
                f"already have. {remaining} expansion(s) left.",
                messages,
            )
        if isinstance(answer, KnowledgeRequestFailed):
            return with_operator_messages(
                f"The expansion could not run — {answer.reason}. This says nothing "
                f"about the game. {remaining} expansion(s) left.",
                messages,
            )
        # Same split as a search: what the expansion showed is glimpsed, not read.
        state.remember_glimpsed(answer.neighbors)
        return with_operator_messages(render_expansion(answer, remaining), messages)

    return [search_knowledge, expand_knowledge]
