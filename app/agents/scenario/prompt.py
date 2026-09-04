import json

from langchain_core.messages import BaseMessage, HumanMessage

from app.agents.scenario.cases import render_game_shape, render_test_case_list
from app.agents.scenario.schemas import OutputLanguage, ScenarioAgentRequest
from app.prompts import PromptFile, load_prompt

# Directory under app/prompts/ holding this agent's prompt versions.
PROMPT_AGENT = "scenario"


# Written in the target language on purpose: the language a directive is written
# in is the strongest signal for the output language. Keep one entry per
# OutputLanguage member.
LANGUAGE_DIRECTIVES: dict[OutputLanguage, str] = {
    OutputLanguage.ko: (
        "출력의 모든 자연어 값(message, 각 시나리오의 title·description, 각 스텝의 action)을 "
        "한국어로 작성한다. JSON 키와 case_id의 숫자는 바꾸지 않는다."
    ),
    OutputLanguage.en: (
        "Write every natural-language value (message, each scenario's title and "
        "description, and each step's action) in English. Do not change JSON keys "
        "or the case_id numbers."
    ),
}


def _hop_text(hop) -> str:
    """hop 하나를 agent 가 그대로 따라 할 문장으로 옮긴다.

    네 종류가 각각 다른 것을 요구한다 — 아무것도 안 함, `bridge` 스텝, 사람이 플레이함,
    사용자에게 되묻기. 한 문구로 뭉개면 agent 는 그 넷 중 어느 것인지 다시 짐작하게 된다.
    """
    if hop.link == "beside":
        return "nothing in between — write the next case straight after"
    if hop.link == "by_operation":
        steps = "; ".join(hop.actions) if hop.actions else "an operation the map knows"
        return f"bridge steps (case_id null): {steps}"
    if hop.link == "by_play":
        steps = "; ".join(hop.actions) if hop.actions else ""
        played = "someone has to play through this"
        return f"{played} — {steps}" if steps else played
    if hop.link == "blocked":
        stops = hop.blocked_by or "unknown"
        return (
            f"no route — {stops} stops it. Do not invent steps: say so in `message` "
            "and ask the user how it is done."
        )
    return "not checked — ask with find_path before writing steps in between"


def render_flows(flows: list) -> str:
    """The walkable flows, one block each (ARTEL-658).

    Kept short on purpose. The case list right above already says what each case is;
    repeating any of it here would double the block that the prompt cache pays for and
    give the model two places to read the same fact from.

    **The chain stays, and it was tested (ARTEL-671).** Printing the order reads as a
    script — measured, the model reproduced six flows and 43 cases byte-identical and
    asked about no other order. So it was replaced with an unordered set and the game's
    shape above was made the ground for ordering. The model then did reorder: five of
    six flows changed. It ordered worse. The scenario that had opened at the title
    screen and walked title → map → story → battle now opened on the **ending screen**,
    and the backwards-reading pairs went from four to five. Grounds were not what was
    missing; it has the board now and still starts at the end.

    Empty renders as a sentence rather than nothing, because "there are no flows" and
    "the flows block is missing" have to look different to whoever reads the prompt back.
    """
    if not flows:
        return "(none — group and order the cases yourself, by the rules below)"
    lines = []
    for index, flow in enumerate(flows, 1):
        opening = ", ".join(flow.opening) if flow.opening else "nothing"
        lines.append(f"flow {index} — starts with: {opening}, gaps: {flow.gaps}")
        if not flow.hops:
            lines.append("  " + " → ".join(str(case_id) for case_id in flow.case_ids))
            continue
        # hop 하나에 한 줄씩. 사이에 무엇이 들어가는지를 순서를 읽는 자리에서 함께 읽게
        # 한다. 화살표만 있을 때는 "이것들이 이어진다"가 전부라, agent 가 나머지를 알려고
        # 이웃마다 `find_path` 를 불렀다 — 실측 한 turn 에 280번 왕복했고, 그 답은 turn 이
        # 시작되기 전에 이미 계산돼 있던 것이다. 적어 두면 물을 것이 남지 않는다.
        lines.append(f"  starts at case {flow.case_ids[0]}")
        for hop in flow.hops:
            lines.append(f"  {hop.from_case_id} → {hop.to_case_id}: {_hop_text(hop)}")
    return "\n".join(lines)


def build_system_prompt(request: ScenarioAgentRequest) -> tuple[str, str]:
    """The system prompt body and the resolved version it came from.

    The project's whole test case list is rendered in here rather than into the
    turn's human message (where `game_context` sits) — on purpose, and it is the
    only reason this is worth doing at all. Providers cache a prompt by its
    prefix, and the message list is ``[system, *history, this turn]``: the system
    block is the one part that is byte-identical on every turn of a session, so a
    list placed there is paid for once. The same text in the turn's message is
    at the end of the list, after everything cacheable, and would be re-billed in
    full on every single turn.

    That is also why the test case list is a session snapshot upstream and why nothing
    here re-sorts it — see `cases.render_test_case_list`.
    """
    system: PromptFile = load_prompt(PROMPT_AGENT, "system")
    body = system.body.format(
        game_shape=render_game_shape(request.test_case_list, request.entry_scene),
        test_case_list=render_test_case_list(request.test_case_list),
        flows=render_flows(request.flows),
        language_directive=LANGUAGE_DIRECTIVES[request.locale],
    )
    return body, system.version


def build_first_message(request: ScenarioAgentRequest) -> str:
    """The turn's opening user message: the goal and the context to author from."""
    human: PromptFile = load_prompt(PROMPT_AGENT, "human")
    return human.body.format(
        unity_context=json.dumps(request.unity_context, ensure_ascii=False),
        game_context=json.dumps(request.game_context, ensure_ascii=False),
        draft=request.draft.model_dump_json() if request.draft is not None else "null",
        current_scenarios=json.dumps(
            [scenario.model_dump() for scenario in request.current_scenarios],
            ensure_ascii=False,
        ),
        user_input=request.user_input,
    )


def build_messages(request: ScenarioAgentRequest) -> list[BaseMessage]:
    """The full message list handed to the agent: replayed history, then the goal.

    Scenarios are not replayed — only the text of past turns (already windowed by
    the session layer) — the same as the previous chain's ``MessagesPlaceholder``.
    """
    return [*request.history, HumanMessage(build_first_message(request))]
