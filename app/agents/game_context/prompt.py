import json

from langchain_core.prompts import ChatPromptTemplate

from app.agents.game_context.schemas import GameContextAgentRequest


SYSTEM_PROMPT = (
    "You are a game design document extraction agent. Read one game design "
    "document and extract structured, reusable game facts for a QA-testing "
    "knowledge base. "
    "INCLUDE game behavior and rules, screens/scenes and their UI elements and "
    "transitions, entities (characters, enemies, items), progression "
    "(levels/stages and their order), notable flows (e.g. tutorials), and "
    "domain terms. "
    "EXCLUDE development-process noise: schedules and deadlines, task/owner "
    "assignments, asset-store links, meeting notes, and team logistics. "
    "Use the FIXED section frame only — never invent new top-level sections. "
    "Put game-specific variety INSIDE entries (rules, attributes, steps), not as "
    "new sections. Use `misc` only for a fact that fits no other section. "
    "Record only what the document supports; do not invent details, and leave a "
    "field or section empty when the document does not cover it. "
    "Write values in the document's own language. Return only valid JSON "
    "matching the requested output contract."
)

HUMAN_TEMPLATE = (
    "Game design document:\n{document_text}\n\n"
    "Output contract (shape guidance; omit empty sections):\n{output_contract}"
)

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


def build_game_context_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            ("human", HUMAN_TEMPLATE),
        ]
    )


def build_chain_inputs(request: GameContextAgentRequest) -> dict:
    return {
        "document_text": request.document_text,
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False),
    }
