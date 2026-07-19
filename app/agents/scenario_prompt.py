import json

from app.agents.scenario_schemas import ScenarioAgentRequest
from app.llm.schemas import LLMMessage


class ScenarioPromptBuilder:
    def build(self, request: ScenarioAgentRequest) -> list[LLMMessage]:
        context_json = request.context.model_dump_json()
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
                        "action": "Concrete action the Unity game QA tester should perform",
                        "expected": "Expected game behavior or observable result",
                    }
                ],
            },
        }

        return [
            LLMMessage(
                role="system",
                content=(
                    "You are a Unity game QA test scenario generation agent. "
                    "Use the provided context and draft to create or revise "
                    "a game QA test scenario. Return only valid JSON that "
                    "matches the requested output contract. Number steps "
                    "sequentially from 1."
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    "Context:\n"
                    f"{context_json}\n\n"
                    "Current draft:\n"
                    f"{draft_json}\n\n"
                    "User input:\n"
                    f"{request.user_input}\n\n"
                    "Output contract:\n"
                    f"{json.dumps(output_contract, ensure_ascii=False)}"
                ),
            ),
        ]
