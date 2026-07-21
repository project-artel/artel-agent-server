import json

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.agents.scenario_schemas import ScenarioAgentRequest


SYSTEM_PROMPT = (
    "You are a Unity game QA test scenario generation agent. "
    "Use the provided game context and conversation to create or revise a game "
    "QA test scenario. The provided draft is the AUTHORITATIVE current state and "
    "may already contain the user's manual edits — preserve those edits and "
    "apply the new user input on top of them; do not discard or silently revert "
    "them. If the draft is null, create a new scenario from scratch. Return only "
    "valid JSON matching the requested output contract, and number steps "
    "sequentially starting from 1."
)

HUMAN_TEMPLATE = (
    "Unity context:\n{unity_context}\n\n"
    "Game context:\n{game_context}\n\n"
    "Current draft (authoritative):\n{draft}\n\n"
    "User input:\n{user_input}\n\n"
    "Output contract:\n{output_contract}"
)

OUTPUT_CONTRACT = {
    "message": "Brief chatbot response for the user.",
    "scenario": {
        "title": "Scenario name",
        "description": "Scenario purpose and summary",
        "steps": [
            {
                "step": 1,
                "title": "Step name",
                "state": (
                    "Starting situation or precondition before the action "
                    "(e.g. title screen, shop screen, must hold at least N gold)"
                ),
                "action": "Concrete action the Unity game QA tester should perform",
                "expected": "Expected game behavior or observable result",
            }
        ],
    },
}


def build_scenario_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            MessagesPlaceholder("history"),
            ("human", HUMAN_TEMPLATE),
        ]
    )


def build_chain_inputs(request: ScenarioAgentRequest) -> dict:
    return {
        "history": request.history,
        "unity_context": json.dumps(request.unity_context, ensure_ascii=False),
        "game_context": json.dumps(request.game_context, ensure_ascii=False),
        "draft": request.draft.model_dump_json() if request.draft is not None else "null",
        "user_input": request.user_input,
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False),
    }
