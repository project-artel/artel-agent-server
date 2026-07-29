import json

from langchain_core.prompts import ChatPromptTemplate

from app.agents.qa.schemas import (
    QaActRequest,
    QaChatRequest,
    QaChatTurn,
    QaVerifyRequest,
)
from app.agents.scenario import OutputLanguage


# Written in the target language on purpose (see scenario.prompt for the rationale).
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: "모든 자연어 출력(thought, message, reasoning)을 한국어로 작성한다.",
    OutputLanguage.en: "Write every natural-language output (thought, message, reasoning) in English.",
}


ACT_SYSTEM = (
    "You are a QA execution agent driving a live Unity game to execute ONE step "
    "of an approved test scenario. You receive the CURRENT scene: interactables "
    "(each with id, name, and type such as button / editText / text, plus `rect` "
    "and `onScreen` when the game reports where they are), visuals — elements "
    "with no invokable action, such as backgrounds and sprites, carrying the same "
    "`rect` so the pointer can still reach them — plus "
    "observables — the tracked state values and recorded action telemetry the "
    "game exposes. Read this scene and carry out the step's natural-language "
    "`action` with an SDK invokable method. Currently available methods (the set "
    "keeps growing — prefer whichever fits the action):\n"
    "- button_click, params [targetId]: click a button element.\n"
    "- enter_text, params [targetId, value]: type value into an editText element.\n"
    "- key_click, params [keyCode, durationSeconds]: press a key (no element target).\n"
    "- move_mouse, params [x, y]: move the pointer to a screen pixel (no element "
    "target). An element's `rect` gives its top-left corner and size in the SAME "
    "pixels this method takes, so aim at the centre — x + w/2, y + h/2 — and pass "
    "it unchanged; never flip an axis. An element with `onScreen` false is not "
    "there to be aimed at.\n"
    "- mouse_down / mouse_up, params [button]: hold and release a mouse button "
    "at the pointer's current position — 0 left, 1 right, 2 middle. A press must "
    "always be matched by its release.\n"
    "- key_down / key_up, params [keyCode]: hold and release a key. Same rule — "
    "never leave a key down.\n"
    "Choose the method and target id actively from THIS scene; target ids must "
    "belong to elements present in the scene — never invent ids. The recorded "
    "action telemetry and states in the scene are OBSERVATIONS for grounding and "
    "verification, NOT things you can invoke. Put the element id in `target_id` "
    "(null when the method takes none, e.g. key_click) and remaining literal args "
    "in `arguments`. Return one concise `thought`, a short user-facing "
    "`action_message`, and the `actions` to perform.\n"
    "You may also decide the scene is not ready to act on — a loading screen, an "
    "animation still playing, a countdown. In that case return NO actions and set "
    "`wait_seconds` to how long to wait before looking again. Prefer acting when "
    "the step's action is possible now; wait only when acting would hit the wrong "
    "screen. You will be shown the scene again after the wait. "
    "{language_directive} Return only valid JSON matching the output contract."
)

ACT_HUMAN = (
    "Scenario: {scenario_title} — {scenario_description}\n\n"
    "Current step {step_number}:\n"
    "- state: {state}\n- action: {action}\n- expected: {expected}\n\n"
    "Current scene:\n{game_state}\n\n"
    "Operator conversation (most recent last). Their instructions take precedence "
    "over your own plan for this step, but never over the scenario's `expected`:\n"
    "{chat}\n\n"
    "Output contract:\n{output_contract}"
)

ACT_OUTPUT_CONTRACT = {
    "thought": "One concise sentence on what you observe and will do next.",
    "action_message": "Short user-facing description of the action, or of what you are waiting for.",
    "wait_seconds": (
        "null to act now, or seconds to wait before looking at the scene again "
        "(leave `actions` empty when waiting)"
    ),
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
    "Operator conversation (most recent last). Weigh what they report, but the "
    "verdict must still follow the evidence and the step's `expected`:\n{chat}\n\n"
    "Output contract:\n{output_contract}"
)

EVAL_OUTPUT_CONTRACT = {
    "reasoning": "One sentence citing the observed evidence.",
    "passed": "true if the expected result held, else false",
    "verdict_message": "Short user-facing verdict.",
}


CHAT_SYSTEM = (
    "You are the QA execution agent, mid-run, answering the operator watching "
    "this run. Answer from the run's own evidence: the scenario, the step you are "
    "on, and the current scene. Be brief and concrete — one short paragraph at "
    "most. If they gave an instruction, acknowledge exactly what you will do "
    "differently on the next step; do not restate the whole plan. Never claim an "
    "action was performed here — this turn only talks, the execution loop acts. "
    "{language_directive} Return only valid JSON matching the output contract."
)

CHAT_HUMAN = (
    "Scenario: {scenario_title} — {scenario_description}\n\n"
    "Current step:\n{step}\n\n"
    "Current scene:\n{game_state}\n\n"
    "Conversation (most recent last; the final USER turn is what you answer):\n"
    "{chat}\n\n"
    "Output contract:\n{output_contract}"
)

CHAT_OUTPUT_CONTRACT = {
    "reply": "Short answer to the operator's latest message.",
}


def build_act_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", ACT_SYSTEM), ("human", ACT_HUMAN)]
    )


def build_evaluate_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", EVAL_SYSTEM), ("human", EVAL_HUMAN)]
    )


def build_chat_prompt() -> ChatPromptTemplate:
    return ChatPromptTemplate.from_messages(
        [("system", CHAT_SYSTEM), ("human", CHAT_HUMAN)]
    )


def render_chat(turns: list[QaChatTurn]) -> str:
    """Flatten the conversation into the prompt. Empty reads as such, not as '[]'."""
    if not turns:
        return "(none)"
    return "\n".join(f"{turn.role}: {turn.message}" for turn in turns)


def build_act_inputs(request: QaActRequest) -> dict:
    return {
        "scenario_title": request.scenario_title,
        "scenario_description": request.scenario_description,
        "step_number": request.step.step,
        "state": request.step.state,
        "action": request.step.action,
        "expected": request.step.expected,
        "game_state": json.dumps(request.game_state.model_dump(), ensure_ascii=False),
        "chat": render_chat(request.chat),
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
        "chat": render_chat(request.chat),
        "output_contract": json.dumps(EVAL_OUTPUT_CONTRACT, ensure_ascii=False),
        "language_directive": LANGUAGE_DIRECTIVES[request.language],
    }


def build_chat_inputs(request: QaChatRequest) -> dict:
    return {
        "scenario_title": request.scenario_title,
        "scenario_description": request.scenario_description,
        "step": (
            "(the run has no step in flight)"
            if request.step is None
            else json.dumps(request.step.model_dump(), ensure_ascii=False)
        ),
        "game_state": (
            "(no scene received yet)"
            if request.game_state is None
            else json.dumps(request.game_state.model_dump(), ensure_ascii=False)
        ),
        "chat": render_chat(request.chat),
        "output_contract": json.dumps(CHAT_OUTPUT_CONTRACT, ensure_ascii=False),
        "language_directive": LANGUAGE_DIRECTIVES[request.language],
    }
