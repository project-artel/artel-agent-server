"""Search queries generated for a knowledge item, so the item can be found.

The retrieval key is not the item's own text. A QA engineer types a question
("골드 모자라면 어떻게 되나") and the item is a statement ("구매는 소지금이 가격
이상일 때 가능하다"); the two sit apart in embedding space. Generating the
questions that would find an item, and embedding those, puts both sides of the
match in the same shape.

``QUESTIONS_PER_ITEM`` is 3 because this stage produces no vector for the item's
own text — there is no fallback behind the generated questions. One question
that misses makes its item unreachable, and finding out why means a human
reading the generated text. Three lowers the odds that all of them miss.
"""

from pydantic import BaseModel, Field

from app.llm.models import DEFAULT_MODEL, LLMModel

QUESTIONS_PER_ITEM = 3


class KnowledgeItem(BaseModel):
    # Echoed back untouched so a batch response can be matched to its request
    # without relying on list order. Opaque here — the agent server does not
    # know or care what Orchestration keys knowledge rows by.
    id: str
    summary: str
    description: str = ""


class KnowledgeQueries(BaseModel):
    """One item's generated questions. The model's structured output shape."""

    queries: list[str] = Field(default_factory=list)


class KnowledgeItemQueries(BaseModel):
    id: str
    queries: list[str]


class KnowledgeQueryAgentRequest(BaseModel):
    item: KnowledgeItem
    model: LLMModel = DEFAULT_MODEL
