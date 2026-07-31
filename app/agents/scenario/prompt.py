import json

from langchain_core.messages import BaseMessage, HumanMessage

from app.agents.scenario.schemas import OutputLanguage, ScenarioAgentRequest
from app.prompts import PromptFile, load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "scenario"


# Written in the target language on purpose: the language a directive is written
# in is the strongest signal for the output language. Keep one entry per
# OutputLanguage member.
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: (
        "출력의 모든 자연어 값(message, 그리고 각 시나리오의 title과 description)을 "
        "한국어로 작성한다. JSON 키와 case_ids의 숫자는 바꾸지 않는다."
    ),
    OutputLanguage.en: (
        "Write every natural-language value (message, and each scenario's title "
        "and description) in English. Do not change JSON keys or the numbers in "
        "case_ids."
    ),
}


def build_system_prompt(request: ScenarioAgentRequest) -> tuple[str, str]:
    """The system prompt body and the resolved version it came from."""
    system: PromptFile = load_prompt(PROMPT_AGENT, "system")
    body = system.body.format(language_directive=LANGUAGE_DIRECTIVES[request.locale])
    return body, system.version


def build_first_message(request: ScenarioAgentRequest) -> str:
    """The turn's opening user message: the goal and the context to author from."""
    human: PromptFile = load_prompt(PROMPT_AGENT, "human")
    return human.body.format(
        unity_context=json.dumps(request.unity_context, ensure_ascii=False),
        game_context=json.dumps(request.game_context, ensure_ascii=False),
        draft=request.draft.model_dump_json() if request.draft is not None else "null",
        user_input=request.user_input,
    )


def build_messages(request: ScenarioAgentRequest) -> list[BaseMessage]:
    """The full message list handed to the agent: replayed history, then the goal.

    Scenarios are not replayed — only the text of past turns (already windowed by
    the session layer) — the same as the previous chain's ``MessagesPlaceholder``.
    """
    return [*request.history, HumanMessage(build_first_message(request))]
