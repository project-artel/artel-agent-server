"""What the planner and the evaluator read, from `app/prompts/qa_planner/<version>/`.

| piece          | source                                                  | read by   |
|----------------|---------------------------------------------------------|-----------|
| rules          | `rules.md`                                              | planner   |
| macro grammar  | `app/agents/qa/macro/reference.py`, in `rules`          | planner   |
| evaluator      | `evaluator.md`                                          | evaluator |
| game map       | `scene_context`: scenes and what the code lets a player do | planner |
| developer macros | the project's developer macros that parse, with their build | planner |
| the step       | the scenario's step, the current screen                 | both      |

`rules` 의 `{macro_grammar}` 자리에는 tool loop agent 가 읽는 것과 같은 macro 문법 글
(`reference.GRAMMAR`·`NUMBER_EQUALITY`)이 들어간다. 그 글이 `skill_macro.md` 에 글자 그대로 있다는
것은 `tests/test_qa_macro_reference.py` 가 지키므로, 두 engine 이 같은 문법 글을 읽는다. 사본을
prompt 파일에 두지 않는 이유는 macro package 가 바뀌는 중이라 사본이 parser 와 어긋나기 때문이다.
대신 그 글의 hash 가 `prompt_hashes["macro_grammar"]` 로 남는다.
"""

import hashlib
import json
from collections.abc import Iterable

from app.agents.planner_executer.developer_macros import UsableDeveloperMacro
from app.agents.qa.macro.model import MacroDefinition
from app.agents.qa.macro.reference import GRAMMAR, NUMBER_EQUALITY
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.scenario import OutputLanguage
from app.prompts import load_prompt, resolve_version, roles_in
from app.qa.pulse import PulseMemory
from app.qa.scene_context import SceneCapability, SceneContext
from app.qa.schemas import QaScenario, QaStep

PROMPT_AGENT = "qa_planner"
RULES_ROLE = "rules"
EVALUATOR_ROLE = "evaluator"
# FREE 시나리오의 씬 계획과 씬 판정(ARTEL-1149). v3 부터 있다.
SCENE_RULES_ROLE = "scene_rules"
SCENE_EVALUATOR_ROLE = "scene_evaluator"

MACRO_GRAMMAR_PLACEHOLDER = "macro_grammar"
MACRO_GRAMMAR = f"{GRAMMAR}\n\n{NUMBER_EQUALITY}"
_FILLS = {MACRO_GRAMMAR_PLACEHOLDER: MACRO_GRAMMAR}

# Lines per scene, and characters per line, of the game map.
MAX_CAPABILITIES_PER_SCENE = 15
MAX_LINE_CHARS = 200


def static_pieces(version: str | None, language: OutputLanguage = OutputLanguage.ko) -> tuple[str, dict[str, str]]:
    """The resolved prompt version and the hash of every piece a run of it reads."""
    resolved = resolve_version(PROMPT_AGENT, version)
    files = {role: load_prompt(PROMPT_AGENT, role, resolved) for role in roles_in(PROMPT_AGENT, resolved)}
    hashes = {role: prompt.body_sha256 for role, prompt in files.items()}
    hashes["language"] = hashlib.sha256(language_directive(language).encode()).hexdigest()
    for name in sorted({name for prompt in files.values() for name in prompt.placeholders}):
        hashes[name] = hashlib.sha256(_FILLS[name].encode()).hexdigest()
    return resolved, hashes


def language_directive(language: OutputLanguage) -> str:
    return LANGUAGE_DIRECTIVES[language] + (
        "\nApply this to explanations, verdict reasons, deferral reasons and macro docstrings. "
        "Preserve code identifiers, selectors, scene names, game text and comparison literals exactly."
    )


def prompt_text(role: str, version: str, language: OutputLanguage = OutputLanguage.ko) -> str:
    """One prompt file's text, its placeholders filled.

    Prompt bodies double their literal braces (`app/prompts/loader.py`); formatting turns them
    back into one.
    """
    prompt = load_prompt(PROMPT_AGENT, role, version)
    return prompt.body.format(**{name: _FILLS[name] for name in prompt.placeholders}) + "\n\n" + language_directive(language)


def screen(pulse: PulseMemory) -> str:
    """The current screen, as the tool loop's agent reads it whole (`PulseMemory.render`)."""
    return pulse.render(since=0, advance=False) or "(the game has sent no reading yet)"


def step_text(item: QaStep) -> str:
    """What the step asks a player to do. A FREE step has no `action`; its test case's step is it."""
    if item.action.strip():
        return item.action.strip()
    if item.case and item.case.test_step:
        return item.case.test_step.strip()
    return "(no action)"


def this_step(scenario: QaScenario, step: int) -> str:
    """The step as JSON: what to do, and for a test case's last step the result to verify."""
    item = scenario.steps[step - 1]
    following = scenario.steps[step].case_id if step < len(scenario.steps) else None
    shown: dict = {"step": step, "do": step_text(item)}
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


def case_view(scenario: QaScenario, step: int) -> dict:
    """One FREE case as the scene planner and the scene evaluator read it."""
    item = scenario.steps[step - 1]
    case = item.case
    shown: dict = {"case_id": item.case_id}
    if case and case.scene:
        shown["scene"] = case.scene
    if case and case.precondition:
        shown["precondition"] = case.precondition
    shown["do"] = step_text(item)
    shown["verify"] = case.expected if case else ""
    return shown


def candidate_list(scenario: QaScenario, steps: list[int]) -> str:
    """The candidate cases of a scene plan, as JSON."""
    return json.dumps([case_view(scenario, step) for step in steps], ensure_ascii=False, indent=2)


def travel_step(step: int, scene: str) -> str:
    """A FREE scenario's move to another scene, written as an ordinary step for the rules prompt."""
    return json.dumps(
        {"step": step, "do": f"Go to the scene `{scene}`.", "verify": f"The game shows the scene `{scene}`."},
        ensure_ascii=False,
        indent=2,
    )


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


def developer_macro_list(usable: Iterable[UsableDeveloperMacro]) -> str | None:
    """프로젝트의 developer macro: 호출 모양, 기준 build, docstring. 없으면 `None`.

    build 로 거르지 않으므로 이 런의 build 가 아닌 build 를 기준으로 쓴 macro 도 실린다. 그런
    macro 에는 그 사실만 적는다 — 쓸지는 planner 가 화면과 docstring 으로 정한다. 고르는 법과
    reuse-first 규칙은 `qa_planner/v2/rules.md` 에 있고, 이 글은 그와 같은 말만 짧게 한다.
    """
    usable = list(usable)
    if not usable:
        return None
    lines = [
        "Developer macros of this project, written by its developers. Read them before you write "
        "an action, and select the ones this step needs in the ```developer block, one per line "
        "— the name alone, or a call with its arguments:"
    ]
    for item in usable:
        macro = item.macro
        build = macro.build_version + (f" ({macro.build_label})" if macro.build_label else "")
        written = f"written against build {build}"
        if macro.other_build:
            written += ", a different build from this run's"
        lines.append(f"- `{_call(item.definition)}` — {written}")
        docstring = item.definition.entry.docstring or ""
        lines.extend(f"    {line}" if line else "" for line in docstring.splitlines())
    return "\n".join(lines)


def _call(definition: MacroDefinition) -> str:
    parameters = []
    for parameter in definition.entry.parameters:
        text = f"{parameter.name}: {parameter.declared_type.value}"
        if parameter.default is not None:
            text += f" = {parameter.default.value!r}"
        parameters.append(text)
    return f"{definition.name}({', '.join(parameters)})"


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
