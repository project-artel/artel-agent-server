import json

from langchain_core.prompts import ChatPromptTemplate

from app.agents.game_context.schemas import GameContextAgentRequest
from app.prompts import load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "game_context"


OUTPUT_CONTRACT = {
    "overview": {
        "title": "Game title",
        "genre": "Genre",
        "platform": "Target platform",
        "summary": "One or two sentence summary",
        "core_loop": "The core gameplay loop",
    },
    "screens": [
        {
            "name": "Screen/scene name",
            "purpose": "What this screen is for",
            "elements": ["Key UI elements on the screen"],
            "transitions": ["How the player enters/leaves this screen"],
        }
    ],
    "mechanics": [
        {
            "name": "Mechanic/system name",
            "description": "What it does",
            "rules": ["Concrete rules or numbers"],
            "preconditions": ["Conditions required for it to apply"],
        }
    ],
    "entities": [
        {
            "name": "Character/enemy/item name",
            "type": "e.g. player, enemy, item",
            "attributes": ["Free-form traits as 'key: value' (e.g. 'weakness: fire')"],
        }
    ],
    "progression": [
        {"name": "Level/stage name", "order": 1, "notes": "Notes about this step"}
    ],
    "flows": [
        {"name": "Flow name (e.g. tutorial)", "steps": ["Ordered steps in the flow"]}
    ],
    "glossary": [{"term": "Domain term", "meaning": "Its meaning in this game"}],
    "misc": [{"note": "A fact that fits none of the sections above"}],
}


def build_game_context_prompt(version: str | None = None) -> ChatPromptTemplate:
    system = load_prompt(PROMPT_AGENT, "system", version)
    human = load_prompt(PROMPT_AGENT, "human", version)
    return ChatPromptTemplate.from_messages(
        [
            ("system", system.body),
            ("human", human.body),
        ]
    )


def build_chain_inputs(request: GameContextAgentRequest) -> dict:
    return {
        "document_text": request.document_text,
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False),
    }
