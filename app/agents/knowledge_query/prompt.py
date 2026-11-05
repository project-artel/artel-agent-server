import json

from langchain_core.prompts import ChatPromptTemplate

from app.agents.knowledge_query.schemas import (
    QUESTIONS_PER_ITEM,
    KnowledgeQueryAgentRequest,
)
from app.prompts import load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "knowledge_query"


OUTPUT_CONTRACT = {
    "queries": [
        "A question a QA engineer would type to find this item, "
        "written in the item's own language"
    ]
}


def build_knowledge_query_prompt(version: str | None = None) -> ChatPromptTemplate:
    system = load_prompt(PROMPT_AGENT, "system", version)
    human = load_prompt(PROMPT_AGENT, "human", version)
    return ChatPromptTemplate.from_messages(
        [
            ("system", system.body),
            ("human", human.body),
        ]
    )


def build_chain_inputs(request: KnowledgeQueryAgentRequest) -> dict:
    return {
        "question_count": str(QUESTIONS_PER_ITEM),
        "summary": request.item.summary,
        # Placeholders are substituted verbatim, so an empty description would
        # leave a bare label under which the model tends to invent detail. Say
        # plainly that there is none.
        "description": request.item.description.strip() or "(none)",
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False),
    }
