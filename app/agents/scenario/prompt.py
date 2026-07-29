import json

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from app.agents.scenario.schemas import OutputLanguage, ScenarioAgentRequest
from app.prompts import load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "scenario"


# Written in the target language on purpose: the language a directive is written
# in is the strongest signal for the output language, especially when the result
# is forced through structured output. Keep one entry per OutputLanguage member.
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: (
        "출력의 모든 자연어 값(message, scenario의 title과 description, 그리고 각 "
        "step의 title/state/action/expected)을 한국어로 작성한다. JSON 키와 step "
        "번호는 바꾸지 않는다."
    ),
    OutputLanguage.en: (
        "Write every natural-language value (message, the scenario title and "
        "description, and each step's title/state/action/expected) in English. "
        "Do not change JSON keys or step numbers."
    ),
}

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
                "action": (
                    "Concrete action the QA tester should perform, in plain "
                    "natural language (no code identifiers)"
                ),
                "expected": "Expected game behavior or observable result",
            }
        ],
    },
}


def build_scenario_prompt(version: str | None = None) -> ChatPromptTemplate:
    system = load_prompt(PROMPT_AGENT, "system", version)
    human = load_prompt(PROMPT_AGENT, "human", version)
    return ChatPromptTemplate.from_messages(
        [
            ("system", system.body),
            MessagesPlaceholder("history"),
            ("human", human.body),
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
        # Direct dict access (not .get): a missing directive must fail loudly
        # rather than silently defaulting the output to another language.
        "language_directive": LANGUAGE_DIRECTIVES[request.locale],
    }
