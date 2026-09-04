import asyncio
from collections.abc import Callable

from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import Runnable

from app.agents.base import AgentContext
from app.agents.knowledge_query.errors import KnowledgeQueryGenerationError
from app.agents.knowledge_query.prompt import (
    build_chain_inputs,
    build_knowledge_query_prompt,
)
from app.agents.knowledge_query.schemas import (
    QUESTIONS_PER_ITEM,
    KnowledgeItem,
    KnowledgeItemQueries,
    KnowledgeQueries,
    KnowledgeQueryAgentRequest,
)
from app.llm.chat_model import structured
from app.llm.models import DEFAULT_MODEL, LLMModel


_MAX_ATTEMPTS = 5

StructuredFactory = Callable[[LLMModel], Runnable]


def _default_structured_factory(model: LLMModel) -> Runnable:
    return structured(model, KnowledgeQueries)


class KnowledgeQueryAgent:
    """Generates the questions that should retrieve a knowledge item.

    One model call per item, run concurrently across a batch, rather than one
    call listing every item. Alignment is the reason: a single call has to keep
    N sets of questions attached to the right N items, and nothing in structured
    output enforces that — a shifted list silently indexes every item under its
    neighbour's questions. Per item, an item's questions cannot belong to
    anything else, and one failure is one item rather than the whole batch.

    ``structured_factory`` is injectable so tests can supply a canned runnable
    instead of calling a real model.
    """

    def __init__(self, structured_factory: StructuredFactory | None = None) -> None:
        self._prompt = build_knowledge_query_prompt()
        self._structured_factory = structured_factory or _default_structured_factory

    async def run(
        self,
        request: KnowledgeQueryAgentRequest,
        context: AgentContext,
    ) -> KnowledgeItemQueries:
        structured = self._structured_factory(request.model)
        chain = (self._prompt | structured).with_retry(
            retry_if_exception_type=(OutputParserException,),
            stop_after_attempt=_MAX_ATTEMPTS,
        )
        try:
            generated = await chain.ainvoke(
                build_chain_inputs(request),
                context.trace_config("knowledge-query-generation"),
            )
        except OutputParserException as error:
            raise KnowledgeQueryGenerationError(
                f"Failed to produce valid query JSON for item {request.item.id!r}."
            ) from error

        queries = [query.strip() for query in generated.queries if query.strip()]
        if not queries:
            # An item indexed under nothing is unreachable by search, and the
            # caller storing it would have no way to tell that from a stored
            # row. Fail instead, so the item is retried rather than lost.
            raise KnowledgeQueryGenerationError(
                f"The model returned no usable queries for item {request.item.id!r}."
            )
        # Surplus is trimmed rather than rejected: extra questions are still
        # valid keys, and failing an item over a count the prompt asked for
        # politely would cost more than dropping the tail.
        return KnowledgeItemQueries(
            id=request.item.id, queries=queries[:QUESTIONS_PER_ITEM]
        )

    async def run_batch(
        self,
        items: list[KnowledgeItem],
        context: AgentContext,
        model: LLMModel = DEFAULT_MODEL,
    ) -> list[KnowledgeItemQueries]:
        results = await asyncio.gather(
            *(
                self.run(KnowledgeQueryAgentRequest(item=item, model=model), context)
                for item in items
            )
        )
        return list(results)
