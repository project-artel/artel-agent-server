import asyncio

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents import (
    AgentContext,
    KnowledgeItem,
    KnowledgeQueryAgent,
    KnowledgeQueryAgentRequest,
    KnowledgeQueryGenerationError,
)
from app.agents.knowledge_query.prompt import (
    OUTPUT_CONTRACT,
    build_chain_inputs,
    build_knowledge_query_prompt,
)
from app.agents.knowledge_query.schemas import QUESTIONS_PER_ITEM, KnowledgeQueries


_CTX = AgentContext(session_id="knowledge-queries-1")


def _item(item_id: str = "k-1") -> KnowledgeItem:
    return KnowledgeItem(
        id=item_id,
        summary="골드 부족 시 구매 불가",
        description="구매는 소지 골드가 아이템 가격 이상일 때만 가능하다.",
    )


def _canned(queries: list[str]) -> KnowledgeQueryAgent:
    result = KnowledgeQueries(queries=queries)
    return KnowledgeQueryAgent(
        structured_factory=lambda model: RunnableLambda(lambda _inputs: result)
    )


def _request(item: KnowledgeItem | None = None) -> KnowledgeQueryAgentRequest:
    return KnowledgeQueryAgentRequest(item=item or _item())


def test_agent_returns_the_queries_under_the_item_id() -> None:
    agent = _canned(["골드 없으면 어떻게 돼?", "구매 조건이 뭐야?", "소지금 부족 시 동작"])

    out = asyncio.run(agent.run(_request(), _CTX))

    assert out.id == "k-1"
    assert out.queries == [
        "골드 없으면 어떻게 돼?",
        "구매 조건이 뭐야?",
        "소지금 부족 시 동작",
    ]


def test_agent_trims_surplus_queries_to_the_configured_count() -> None:
    agent = _canned([f"질문 {n}" for n in range(1, 7)])

    out = asyncio.run(agent.run(_request(), _CTX))

    assert len(out.queries) == QUESTIONS_PER_ITEM


def test_agent_drops_blank_queries() -> None:
    agent = _canned(["골드 없으면?", "   ", "구매 조건"])

    out = asyncio.run(agent.run(_request(), _CTX))

    assert out.queries == ["골드 없으면?", "구매 조건"]


def test_agent_fails_when_nothing_usable_comes_back() -> None:
    """An item indexed under no query is unreachable, and silently so."""
    agent = _canned(["", "   "])

    with pytest.raises(KnowledgeQueryGenerationError, match="k-1"):
        asyncio.run(agent.run(_request(), _CTX))


def test_agent_retries_a_parse_failure_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky(_inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OutputParserException("bad json")
        return KnowledgeQueries(queries=["골드 없으면?"])

    agent = KnowledgeQueryAgent(
        structured_factory=lambda model: RunnableLambda(flaky)
    )

    out = asyncio.run(agent.run(_request(), _CTX))

    assert out.queries == ["골드 없으면?"]
    assert calls["n"] == 2


def test_agent_raises_after_exhausting_retries() -> None:
    def always_fail(_inputs):
        raise OutputParserException("still bad")

    agent = KnowledgeQueryAgent(
        structured_factory=lambda model: RunnableLambda(always_fail)
    )

    with pytest.raises(KnowledgeQueryGenerationError):
        asyncio.run(agent.run(_request(), _CTX))


def test_run_batch_calls_the_model_once_per_item_and_keeps_ids_attached() -> None:
    summaries = {"골드", "인벤토리", "길드"}
    seen: list[str] = []

    def per_item(prompt_value):
        rendered = prompt_value.to_string()
        summary = next(name for name in summaries if name in rendered)
        seen.append(summary)
        return KnowledgeQueries(queries=[f"{summary} 관련 질문"])

    agent = KnowledgeQueryAgent(
        structured_factory=lambda model: RunnableLambda(per_item)
    )
    items = [
        KnowledgeItem(id="a", summary="골드", description="d1"),
        KnowledgeItem(id="b", summary="인벤토리", description="d2"),
        KnowledgeItem(id="c", summary="길드", description="d3"),
    ]

    out = asyncio.run(agent.run_batch(items, _CTX))

    # One prompt per item — the alignment risk of a single N-item call is the
    # reason the agent fans out.
    assert sorted(seen) == sorted(summaries)
    assert [result.id for result in out] == ["a", "b", "c"]
    # Each item's questions were generated from that item, not from a neighbour.
    assert out[1].queries == ["인벤토리 관련 질문"]


# --- prompt inputs ------------------------------------------------------------


def test_chain_inputs_carry_the_item_and_the_requested_count() -> None:
    inputs = build_chain_inputs(_request())

    assert inputs["summary"] == "골드 부족 시 구매 불가"
    assert inputs["description"].startswith("구매는 소지 골드가")
    assert inputs["question_count"] == str(QUESTIONS_PER_ITEM)
    assert "queries" in inputs["output_contract"]


def test_chain_inputs_name_a_missing_description_rather_than_leaving_it_blank() -> None:
    item = KnowledgeItem(id="k-2", summary="길드 정원 50명", description="   ")

    inputs = build_chain_inputs(KnowledgeQueryAgentRequest(item=item))

    assert inputs["description"] == "(none)"


def test_output_contract_is_serialised_without_ascii_escaping() -> None:
    inputs = build_chain_inputs(_request())

    assert "\\u" not in inputs["output_contract"]


def test_prompt_asks_for_the_item_language_and_the_configured_count() -> None:
    prompt = build_knowledge_query_prompt()

    rendered = prompt.format(
        question_count=str(QUESTIONS_PER_ITEM),
        summary="s",
        description="d",
        output_contract=str(OUTPUT_CONTRACT),
    )

    assert "SAME LANGUAGE" in rendered
    assert f"exactly {QUESTIONS_PER_ITEM}" in rendered
