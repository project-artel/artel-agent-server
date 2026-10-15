"""What the planner and the evaluator read, from `app/prompts/qa_planner/<version>/`.

| piece          | source                                                  | read by   |
|----------------|---------------------------------------------------------|-----------|
| rules          | `rules.md`                                              | planner   |
| macro grammar  | `app/agents/qa/macro/reference.py`, in `rules`          | planner   |
| evaluator      | `evaluator.md`                                          | evaluator |
| game map       | `scene_context`: scenes and what the code lets a player do | planner |
| the step       | the scenario's step, the current screen                 | both      |

`rules` 의 `{macro_grammar}` 자리에는 tool loop agent 가 읽는 것과 같은 macro 문법 글
(`reference.GRAMMAR`·`NUMBER_EQUALITY`)이 들어간다. 그 글이 `skill_macro.md` 에 글자 그대로 있다는
것은 `tests/test_qa_macro_reference.py` 가 지키므로, 두 engine 이 같은 문법 글을 읽는다. 사본을
prompt 파일에 두지 않는 이유는 macro package 가 바뀌는 중이라 사본이 parser 와 어긋나기 때문이다.
대신 그 글의 hash 가 `prompt_hashes["macro_grammar"]` 로 남는다.
"""

import hashlib
import json

from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY
from app.prompts import load_prompt, resolve_version, roles_in
from app.qa.pulse import PulseMemory
from app.qa.scene_context import SceneCapability, SceneContext
from app.qa.schemas import QaScenario

PROMPT_AGENT = "qa_planner"
RULES_ROLE = "rules"
EVALUATOR_ROLE = "evaluator"

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


def prompt_text(role: str, version: str) -> str:
    """One prompt file's text, its placeholders filled.

    Prompt bodies double their literal braces (`app/prompts/loader.py`); formatting turns them
    back into one.
    """
    prompt = load_prompt(PROMPT_AGENT, role, version)
    return prompt.body.format(**{name: _FILLS[name] for name in prompt.placeholders})


def screen(pulse: PulseMemory) -> str:
    """The current screen, as the tool loop's agent reads it whole (`PulseMemory.render`)."""
    return pulse.render(since=0, advance=False) or "(the game has sent no reading yet)"


def this_step(scenario: QaScenario, step: int) -> str:
    """The step as JSON: what to do, and for a test case's last step the result to verify."""
    item = scenario.steps[step - 1]
    following = scenario.steps[step].case_id if step < len(scenario.steps) else None
    shown: dict = {"step": step, "do": item.action.strip() or "(no action)"}
    if item.hint:
        shown["try"] = item.hint.strip()
    if item.input:
        shown["via"] = item.input.strip()
    if item.case_id is not None:
        if item.case and item.case.precondition:
            shown["precondition"] = item.case.precondition
        if following != item.case_id:
            shown["verify"] = item.case.expected if item.case else ""
    return json.dumps(shown, ensure_ascii=False, indent=2)


def game_map(context: SceneContext | None) -> str | None:
    """Each scene of the build and what its code lets a player do there, or `None`.

    The planner writes for screens it has not seen yet; without the scene names it guessed them —
    two models wrote `scene() == "MapScene"` from a button called `MapSceneButton`; the scene is
    `Map_scene` (PoC).
    """
    if context is None or not context.scenes:
        return None
    lines = [
        "Scenes in this build — the exact names `scene()` will return. Use no other scene name:",
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
