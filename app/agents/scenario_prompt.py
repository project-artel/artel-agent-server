import json

from app.agents.scenario_schemas import ScenarioAgentRequest
from app.llm.schemas import LLMMessage, MessageRole


_SYSTEM_PROMPT = (
    "You are a Unity game QA test scenario generation agent. "
    "Use the provided game context and conversation to create or revise a game "
    "QA test scenario. The provided draft is the AUTHORITATIVE current state and "
    "may already contain the user's manual edits — preserve those edits and "
    "apply the new user input on top of them; do not discard or silently revert "
    "them. If the draft is null, create a new scenario from scratch. Return only "
    "valid JSON matching the requested output contract, and number steps "
    "sequentially starting from 1."
)


class ScenarioPromptBuilder:
    def build(self, request: ScenarioAgentRequest) -> list[LLMMessage]:
        draft_json = (
            request.draft.model_dump_json()
            if request.draft is not None
            else "null"
        )

        output_contract = {
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

        current_turn = (
            "Unity context:\n"
            f"{json.dumps(request.unity_context, ensure_ascii=False)}\n\n"
            "Game context:\n"
            f"{json.dumps(request.game_context, ensure_ascii=False)}\n\n"
            "Current draft (authoritative):\n"
            f"{draft_json}\n\n"
            "User input:\n"
            f"{request.user_input}\n\n"
            "Output contract:\n"
            f"{json.dumps(output_contract, ensure_ascii=False)}"
        )

        messages: list[LLMMessage] = [
            LLMMessage(role=MessageRole.system, content=_SYSTEM_PROMPT)
        ]
        messages.extend(request.history)
        messages.append(LLMMessage(role=MessageRole.user, content=current_turn))
        return messages
