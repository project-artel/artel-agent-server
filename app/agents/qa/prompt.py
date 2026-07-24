import json

from langchain_core.prompts import ChatPromptTemplate

from app.agents.qa.schemas import QaActRequest, QaVerifyRequest
from app.agents.scenario import OutputLanguage


# Written in the target language on purpose (see scenario.prompt for the rationale).
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: "모든 자연어 출력(thought, message, reasoning)을 한국어로 작성한다.",
    OutputLanguage.en: "Write every natural-language output (thought, message, reasoning) in English.",
}


ACT_SYSTEM = (
    "You are a QA execution agent driving a live Unity game to execute ONE step "
    "of an approved test scenario. You receive the CURRENT scene: interactables "
    "(each with id, name, and type such as button / editText / text) plus "
    "observables — the tracked state values and recorded action telemetry the "
    "game exposes. Read this scene and carry out the step's natural-language "
    "`action` with an SDK invokable method. Currently available methods (the set "
    "keeps growing — prefer whichever fits the action):\n"
    "- button_click, params [targetId]: click a button element.\n"
    "- enter_text, params [targetId, value]: type value into an editText element.\n"
    "- key_click, params [keyCode, durationSeconds]: press a key (no element target).\n"
    "Choose the method and target id actively from THIS scene; target ids must "
    "belong to elements present in the scene — never invent ids. The recorded "
    "action telemetry and states in the scene are OBSERVATIONS for grounding and "
    "verification, NOT things you can invoke. Put the element id in `target_id` "
    "(null when the method takes none, e.g. key_click) and remaining literal args "
    "in `arguments`. Return one concise `thought`, a short user-facing "
    "`action_message`, and the `actions` to perform. {language_directive} Return "
    "only valid JSON matching the output contract."
)

ACT_HUMAN = (
    "Scenario: {scenario_title} — {scenario_description}\n\n"
    "Current step {step_number}:\n"
    "- state: {state}\n- action: {action}\n- expected: {expected}\n\n"
    "Current scene:\n{game_state}\n\n"
    "Output contract:\n{output_contract}"
)

ACT_OUTPUT_CONTRACT = {
    "thought": "One concise sentence on what you observe and will do next.",
    "action_message": "Short user-facing description of the action.",
    "actions": [
        {
            "method": "an SDK invokable method, e.g. button_click / enter_text / key_click",
            "target_id": "element id present in the scene (null when the method takes none)",
            "arguments": [
                "literal args after the id: [value] for enter_text, "
                "[keyCode, durationSeconds] for key_click, [] for button_click"
            ],
        }
    ],
}


EVAL_SYSTEM = (
    "You are verifying whether a QA scenario step passed. Compare the step's "
    "natural-language `expected` against the resulting scene observables and the "
    "per-action success/failure results. Decide `passed` (true/false), give a "
    "one-sentence `reasoning` citing the concrete evidence, and a short "
    "user-facing `verdict_message`. {language_directive} Return only valid JSON "
    "matching the output contract."
)

EVAL_HUMAN = (
    "Step {step_number}:\n- action: {action}\n- expected: {expected}\n\n"
    "Action results (success/failure per action):\n{action_result}\n\n"
    "Resulting scene:\n{game_state}\n\n"
    "Output contract:\n{output_contract}"
)

EVAL_OUTPUT_CONTRACT = {
    "reasoning": "One sentence citing the observed evidence.",
    "passed": "true if the expected result held, else false",
    "verdict_message": "Short user-facing verdict.",
}


def build_act_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", ACT_SYSTEM), ("human", ACT_HUMAN)]
    )


def build_evaluate_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", EVAL_SYSTEM), ("human", EVAL_HUMAN)]
    )


def build_act_inputs(request: QaActRequest) -> dict:
    return {
        "scenario_title": request.scenario_title,
        "scenario_description": request.scenario_description,
        "step_number": request.step.step,
        "state": request.step.state,
        "action": request.step.action,
        "expected": request.step.expected,
        "game_state": json.dumps(request.game_state.model_dump(), ensure_ascii=False),
        "output_contract": json.dumps(ACT_OUTPUT_CONTRACT, ensure_ascii=False),
        "language_directive": LANGUAGE_DIRECTIVES[request.language],
    }


def build_evaluate_inputs(request: QaVerifyRequest) -> dict:
    return {
        "step_number": request.step.step,
        "action": request.step.action,
        "expected": request.step.expected,
        "action_result": json.dumps(
            request.action_result.model_dump(), ensure_ascii=False
        ),
        "game_state": json.dumps(request.game_state.model_dump(), ensure_ascii=False),
        "output_contract": json.dumps(EVAL_OUTPUT_CONTRACT, ensure_ascii=False),
        "language_directive": LANGUAGE_DIRECTIVES[request.language],
    }
