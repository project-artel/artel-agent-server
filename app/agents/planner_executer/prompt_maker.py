"""Everything the planner reads, assembled in one place.

| piece          | source                                                  | differs per    |
|----------------|---------------------------------------------------------|----------------|
| rules, api     | `app/prompts/qa_planner/<version>/`                     | prompt version |
| macro grammar  | `app/agents/qa/macro/reference.py`, in `api`            | macro package  |
| game map       | `scene_context`: each scene and what its code lets a player do | build   |
| the step       | the scenario, the step, the current state               | call           |

`api` 의 `{macro_grammar}` 자리에는 tool loop agent 가 읽는 것과 같은 macro 문법 글
(`reference.GRAMMAR`·`NUMBER_EQUALITY`)이 들어간다. 그 글이 `skill_macro.md` 에 글자 그대로 있다는
것은 `tests/test_qa_macro_reference.py` 가 지키므로, 두 engine 이 같은 문법 글을 읽는다. 사본을
prompt 파일에 두지 않는 이유는 macro package 가 바뀌는 중이라 사본이 parser 와 어긋나기 때문이다.
대신 그 글의 hash 가 `prompt_hashes["macro_grammar"]` 로 남는다.
"""

import hashlib
import json

from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY
from app.prompts import PromptFile, load_prompt, resolve_version, roles_in
from app.qa.scene_context import SceneCapability, SceneContext
from app.qa.schemas import QaScenario, QaStep

PROMPT_AGENT = "qa_planner"
RULES_ROLE = "rules"
API_ROLE = "api"

# prompt 파일이 placeholder 로 받는 글. 파일의 frontmatter 가 이름을 선언한 것만 채운다.
MACRO_GRAMMAR_PLACEHOLDER = "macro_grammar"
MACRO_GRAMMAR = f"{GRAMMAR}\n\n{NUMBER_EQUALITY}"
_FILLS = {MACRO_GRAMMAR_PLACEHOLDER: MACRO_GRAMMAR}

# Lines per scene, and characters per line, of the game map.
MAX_CAPABILITIES_PER_SCENE = 15
MAX_LINE_CHARS = 200


def static_pieces(version: str | None) -> tuple[str, dict[str, str]]:
    """The resolved prompt version and the hash of every piece a run of it reads."""
    resolved = resolve_version(PROMPT_AGENT, version)
    files = {role: load_prompt(PROMPT_AGENT, role, resolved) for role in roles_in(PROMPT_AGENT, resolved)}
    hashes = {role: prompt.body_sha256 for role, prompt in files.items()}
    for name in sorted({name for prompt in files.values() for name in prompt.placeholders}):
        hashes[name] = hashlib.sha256(_FILLS[name].encode()).hexdigest()
    return resolved, hashes


class PromptMaker:
    def __init__(self, version: str) -> None:
        self.version = version
        self._rules = load_prompt(PROMPT_AGENT, RULES_ROLE, version)
        self._api = load_prompt(PROMPT_AGENT, API_ROLE, version)

    def system(self) -> str:
        """Rules, then the `g` API with the macro grammar."""
        return f"{_text(self._rules)}\n\n{_text(self._api)}"

    def write_request(
        self,
        *,
        scenario: QaScenario,
        step: int,
        state: str,
        context: SceneContext | None,
        feedback: str = "",
    ) -> str:
        """The user message for writing step `step`."""
        drawn = game_map(context)
        return (
            (f"Game map:\n{drawn}\n\n" if drawn else "")
            + f"Scenario: {scenario.title} — {scenario.description}\n\n"
            + f"All steps, for context:\n{describe_steps(scenario, 1)}\n\n"
            + f"Write step {step} now:\n{this_step(scenario, step)}\n\n"
            + f"Current game state (pulse):\n{state}\n"
            + (f"\n{feedback}\n" if feedback else "")
        )


def game_map(context: SceneContext | None) -> str | None:
    """Each scene of the build and what its code lets a player do there, or `None`.

    The planner writes funcs for screens it has not seen yet; without the scene names it guessed
    them — two models wrote `g.scene == "MapScene"` from a button called `MapSceneButton`; the
    scene is `Map_scene` (PoC).
    """
    if context is None or not context.scenes:
        return None
    lines = [
        "Scenes in this build — the exact names `g.scene` will have. Use no other scene name:",
        "  " + ", ".join(scene.scene_name or "?" for scene in context.scenes),
        "",
        "What the build's code lets a player do in each scene (from a static scan, not yet "
        "verified in play; `when` is the code's own condition):",
    ]
    for scene in context.scenes:
        doable = scene.capabilities
        lines.append(f"[{scene.scene_name}]" + ("" if doable else " (nothing doable found)"))
        if scene.scene_summary:
            lines.append("  " + _clip(scene.scene_summary))
        lines.extend("  - " + _clip(_capability(c)) for c in doable[:MAX_CAPABILITIES_PER_SCENE])
        if len(doable) > MAX_CAPABILITIES_PER_SCENE:
            lines.append(f"  ({len(doable) - MAX_CAPABILITIES_PER_SCENE} more)")
    return "\n".join(lines)


def describe_steps(scenario: QaScenario, first_step: int) -> str:
    """The steps from `first_step` on, in the shape the agent's first message gives them."""
    items = []
    previous_case = None
    steps = scenario.steps
    for index, step in enumerate(steps, start=1):
        case_id = step.case_id
        following = steps[index].case_id if index < len(steps) else None
        is_verification = case_id is not None and following != case_id
        if index >= first_step:
            item: dict = {"step": index, "do": _action_line(step)}
            if case_id is not None:
                item["case_id"] = case_id
                if case_id != previous_case and step.case and step.case.precondition:
                    item["precondition"] = step.case.precondition
                if is_verification:
                    item["verify"] = step.case.expected if step.case else ""
            items.append(item)
        previous_case = case_id
    return json.dumps(items, ensure_ascii=False, indent=2)


def this_step(scenario: QaScenario, step: int) -> str:
    items = json.loads(describe_steps(scenario, step))
    return json.dumps(items[0] if items else {}, ensure_ascii=False, indent=2)


def _action_line(step: QaStep) -> str:
    line = step.action.strip() or "(no action)"
    extras = []
    if step.hint:
        extras.append(f"try: {step.hint.strip()}")
    if step.input:
        extras.append(f"via: {step.input.strip()}")
    return f"{line}  ({'; '.join(extras)})" if extras else line


def _capability(capability: SceneCapability) -> str:
    action = capability.interaction or "?"
    if capability.input_key:
        action += f" {capability.input_key}"
    if capability.control_label:
        action += f' "{capability.control_label}"'
    where = capability.control_path or capability.control_selector_hint
    if where:
        action += f" ({where})"
    text = f"{action} — {capability.summary}" if capability.summary else action
    if capability.given_text:
        text += f"  when {capability.given_text}"
    if capability.repeat_until_done:
        text += "  (repeat until done)"
    return text


def _clip(text: str) -> str:
    text = " ".join(text.replace("`", "").split())
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + "…"


def _text(prompt: PromptFile) -> str:
    # Prompt bodies double their literal braces (`app/prompts/loader.py`); formatting turns them
    # back into one and fills the placeholders the file declares.
    return prompt.body.format(**{name: _FILLS[name] for name in prompt.placeholders})
