import json

from langchain_core.prompts import ChatPromptTemplate

from app.agents.step_phrasing.schemas import StepPhrasingRequest
from app.prompts import load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "step_phrasing"

OUTPUT_CONTRACT = {
    "steps": [
        {
            "action": "One step, in the user's own meaning and the neighbours' voice",
            "input": "key:<KeyName> or click:<control> — the machine reads this, or null",
        }
    ]
}


def build_step_phrasing_prompt(version: str | None = None) -> ChatPromptTemplate:
    system = load_prompt(PROMPT_AGENT, "system", version)
    human = load_prompt(PROMPT_AGENT, "human", version)
    return ChatPromptTemplate.from_messages(
        [
            ("system", system.body),
            ("human", human.body),
        ]
    )


def build_chain_inputs(request: StepPhrasingRequest) -> dict:
    # Placeholders are substituted verbatim, so an empty neighbour would leave a
    # bare label under which the model invents one. Say plainly there is none.
    return {
        "blocked_by": request.blocked_by.strip() or "(not named)",
        "before": request.before.strip() or "(this gap is at the start)",
        "after": request.after.strip() or "(this gap is at the end)",
        "said": request.said.strip(),
        "locale": request.locale,
        "output_contract": json.dumps(OUTPUT_CONTRACT, ensure_ascii=False),
    }
