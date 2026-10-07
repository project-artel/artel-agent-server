"""`load_skill`, the skills axis, and what the folds do to a loaded skill.

The skill bodies are stubbed where a test is about the wiring, so these do not
move when the text of `qa_run/v19/skill_*.md` is edited. The fold tests read the
real files as well, because what they guard against is a real body carrying a
marker the folds look for.
"""

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa import runner as runner_module
from app.agents.qa.arch import default_resolved_arch
from app.agents.qa.context import fold_stale_knowledge, fold_stale_scenes, fold_stale_skills
from app.agents.qa.knowledge import NEIGHBOUR_BLOCK_START_PREFIX
from app.agents.qa.runner import (
    _skills_directive,
    middleware_names_for,
    system_prompt_with_skills,
)
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools import skill_tools
from app.agents.qa.tools.skill_tools import wrap_skill
from app.prompts import load_prompt, load_skill, load_tool_description, skill_names
from app.qa.channel import QaRunChannel
from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX, SCENE_VIEW_START_SUFFIX

SKILLS = {
    "content_map": "## The content map\n\nWrite what you learned.",
    "knowledge_base": "## The knowledge base\n\nSearch before guessing.",
}


@pytest.fixture
def stub_skills(monkeypatch):
    def names(version=None):
        return tuple(sorted(SKILLS))

    def body(name, version=None):
        return SimpleNamespace(body=SKILLS[name])

    monkeypatch.setattr(skill_tools, "skill_names", names)
    monkeypatch.setattr(skill_tools, "load_skill_file", body)
    monkeypatch.setattr(runner_module, "skill_names", names)
    monkeypatch.setattr(runner_module, "load_skill", body)


def make_tools(skills: str) -> dict:
    async def send(frame: dict) -> None:
        pass

    channel = QaRunChannel(qa_try_id=7, send=send)
    arch = default_resolved_arch().model_copy(update={"skills": skills})
    return {tool.name: tool for tool in build_tools(channel, QaRunState(total_steps=1), arch)}


# --- the tool set -------------------------------------------------------------


def test_load_skill_is_offered_only_on_demand(stub_skills) -> None:
    assert "load_skill" not in make_tools("off")
    on_demand = list(make_tools("on_demand"))
    assert on_demand[-1] == "load_skill"
    # The rest of the list is the `off` list, in the same order.
    assert on_demand[:-1] == list(make_tools("off"))


def test_the_description_names_every_skill_and_stays_under_500(stub_skills) -> None:
    description = make_tools("on_demand")["load_skill"].description
    for name in SKILLS:
        assert name in description
    assert len(description) <= 500


def test_the_description_file_is_under_500_characters() -> None:
    assert len(load_tool_description("load_skill", "v19").body) <= 500


def test_load_skill_takes_a_name_and_a_thought(stub_skills) -> None:
    assert set(make_tools("on_demand")["load_skill"].args) == {"name", "thought"}


# --- what the tool returns ----------------------------------------------------


def call(tool, name: str) -> str:
    return asyncio.run(tool.ainvoke({"name": name, "thought": "about to search"}))


def test_load_skill_returns_the_body_between_named_markers(stub_skills) -> None:
    tool = make_tools("on_demand")["load_skill"]
    result = call(tool, "knowledge_base")
    assert result == wrap_skill("knowledge_base", SKILLS["knowledge_base"])
    assert result.startswith("<<skill knowledge_base>>\n")
    assert result.endswith("\n<<end skill knowledge_base>>")


def test_an_unknown_name_lists_the_valid_ones(stub_skills) -> None:
    result = call(make_tools("on_demand")["load_skill"], "held_stat")
    assert "not a skill" in result
    for name in SKILLS:
        assert name in result
    assert SKILLS["knowledge_base"] not in result


def test_a_reload_returns_the_body_again_and_says_so(stub_skills) -> None:
    tool = make_tools("on_demand")["load_skill"]
    call(tool, "content_map")
    again = call(tool, "content_map")
    assert "already loaded" in again
    # The note sits outside the markers, so a fold replaces only the body.
    assert again.index("already loaded") < again.index("<<skill content_map>>")
    assert again.endswith(wrap_skill("content_map", SKILLS["content_map"]))
    # Another skill is still a first load.
    assert call(tool, "knowledge_base") == wrap_skill("knowledge_base", SKILLS["knowledge_base"])


def test_each_run_starts_with_nothing_loaded(stub_skills) -> None:
    call(make_tools("on_demand")["load_skill"], "content_map")
    assert call(make_tools("on_demand")["load_skill"], "content_map") == wrap_skill(
        "content_map", SKILLS["content_map"]
    )


# --- the system prompt --------------------------------------------------------


def test_off_inlines_every_skill_from_v19(stub_skills) -> None:
    prompt = system_prompt_with_skills("# System\n\nHow to work.\n", "v19", "off")
    assert prompt.startswith("# System\n\nHow to work.")
    assert prompt.index(SKILLS["content_map"]) < prompt.index(SKILLS["knowledge_base"])


def test_off_wraps_each_skill_in_a_named_section(stub_skills) -> None:
    prompt = system_prompt_with_skills("# System\n", "v19", "off")
    # The tool descriptions say "the held_state skill", so the section is named that way.
    assert "\n## Skill: content_map\n\n" in prompt
    assert "\n## Skill: knowledge_base\n\n" in prompt


def test_on_demand_leaves_the_v19_prompt_as_written(stub_skills) -> None:
    assert system_prompt_with_skills("# System\n", "v19", "on_demand") == "# System\n"


@pytest.mark.parametrize("skills", ["off", "on_demand"])
def test_a_version_before_v19_is_never_changed(stub_skills, skills) -> None:
    # v18 is develop's phase-cycle prompt: it has no skill files, and an `off` run
    # on it must read exactly what it read before the skills axis existed.
    for version in ("v17", "v18"):
        assert system_prompt_with_skills("# System\n", version, skills) == "# System\n"


def test_a_body_without_a_heading_is_inlined_under_its_section(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "skill_names", lambda version=None: ("held_state",))
    monkeypatch.setattr(
        runner_module, "load_skill", lambda name, version=None: SimpleNamespace(body="Set it once.")
    )
    prompt = system_prompt_with_skills("# System\n", "v19", "off")
    assert "## Skill: held_state\n\nSet it once." in prompt


def test_a_skill_heading_is_pushed_below_its_skill_section_when_inlined(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "skill_names", lambda version=None: ("knowledge_base",))
    monkeypatch.setattr(
        runner_module,
        "load_skill",
        lambda name, version=None: SimpleNamespace(
            body="# The knowledge base\n\nIntro.\n\n## Removing a link\n\nText."
        ),
    )
    prompt = system_prompt_with_skills("# System\n", "v19", "off")
    assert (
        "\n## Skill: knowledge_base\n\n### The knowledge base\n\nIntro.\n\n#### Removing a link\n"
        in prompt
    )
    assert "\n# The knowledge base" not in prompt


def test_the_real_v19_off_prompt_has_one_skill_section_per_skill() -> None:
    body = load_prompt("qa_run", "system", "v19").body
    prompt = system_prompt_with_skills(body, "v19", "off")
    for name in skill_names("v19"):
        assert f"\n## Skill: {name}\n" in prompt
        assert load_skill(name, "v19").body.strip().splitlines()[-1] in prompt
    # Only the system prompt's own first line may be a `#` heading, and v19 has none.
    assert not [line for line in prompt.splitlines() if line.startswith("# ")]


def test_a_brace_in_a_skill_body_is_not_formatted(monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "skill_names", lambda version=None: ("x",))
    monkeypatch.setattr(
        runner_module, "load_skill", lambda name, version=None: SimpleNamespace(body="## X\n\n{literal}")
    )
    assert "{literal}" in system_prompt_with_skills("# System\n", "v19", "off")


# --- the Skills directive -----------------------------------------------------


def test_on_demand_fills_the_directive_from_its_file() -> None:
    directive = _skills_directive("v19", "on_demand")
    assert directive == load_prompt("qa_run", "skills_directive", "v19").body
    assert directive.lstrip().startswith("## Skills")
    assert "load_skill(" in directive


def test_off_fills_the_directive_with_nothing() -> None:
    assert _skills_directive("v19", "off") == ""


def test_a_version_before_v19_has_no_directive() -> None:
    assert _skills_directive("v17", "on_demand") == ""
    assert _skills_directive("v18", "on_demand") == ""


def _assembled(skills: str) -> str:
    """The system prompt as the runner builds it: placeholders filled, then skills inlined.

    The phase directives are filled the way the default `phase_cycle=lite` run
    fills them, so the two axes are checked together.
    """
    template = load_prompt("qa_run", "system", "v19").body
    filled = template.format(
        vision_directive="",
        memory_directive=load_prompt("qa_run", "memory_directive", "v19").body,
        phase_directive=load_prompt("qa_run", "phase_directive", "v19").body,
        decide_directive="",
        skills_directive=_skills_directive("v19", skills),
        language_directive="",
    )
    return system_prompt_with_skills(filled, "v19", skills)


def test_on_demand_prompt_has_the_skills_section() -> None:
    prompt = _assembled("on_demand")
    assert "## Skills" in prompt
    assert "load_skill(" in prompt
    for name in skill_names("v19"):
        assert f"`{name}`" in prompt
    assert "## Skill: " not in prompt


def test_off_prompt_never_mentions_load_skill() -> None:
    prompt = _assembled("off")
    assert "load_skill(" not in prompt
    assert "## Skills\n" not in prompt


@pytest.mark.parametrize("skills", ["off", "on_demand"])
def test_a_phase_cycle_run_keeps_its_directives_on_either_side(skills) -> None:
    prompt = _assembled(skills)
    # `phase_directive` sits before the `thought` paragraph, `memory_directive`
    # closes the scene context paragraphs; both survive the skills axis.
    assert prompt.index("This run holds you to a fixed order") < prompt.index(
        "Every tool takes a `thought`"
    )
    assert "`report_step` takes `capability_key` and `learned` directly" in prompt


def test_off_tool_descriptions_never_mention_load_skill() -> None:
    for name, tool in make_tools("off").items():
        assert "load_skill" not in tool.description, name


# --- folding ------------------------------------------------------------------


def scene_view(at: int) -> str:
    return f"{SCENE_VIEW_START_PREFIX}{at}{SCENE_VIEW_START_SUFFIX}\nscene: Lobby\n{SCENE_VIEW_END}"


def loaded(name: str, body: str) -> str:
    """What `load_skill` returns for a first load."""
    return wrap_skill(name, body)


def conversation(skill_result: str, later_skill: str | None = None) -> list:
    """A skill loaded early, then two acting turns whose views push it back.

    With `later_skill`, a second skill is loaded after the views, so the first one
    is no longer the newest.
    """
    messages = [
        HumanMessage(content="Begin."),
        AIMessage(content="", tool_calls=[{"name": "load_skill", "args": {}, "id": "s"}]),
        ToolMessage(content=skill_result, tool_call_id="s", name="load_skill"),
        AIMessage(content="", tool_calls=[{"name": "observe_scene", "args": {}, "id": "a"}]),
        ToolMessage(content=scene_view(1), tool_call_id="a", name="observe_scene"),
        AIMessage(content="", tool_calls=[{"name": "observe_scene", "args": {}, "id": "b"}]),
        ToolMessage(content=scene_view(2), tool_call_id="b", name="observe_scene"),
    ]
    if later_skill is not None:
        messages += [
            AIMessage(content="", tool_calls=[{"name": "load_skill", "args": {}, "id": "t"}]),
            ToolMessage(content=later_skill, tool_call_id="t", name="load_skill"),
        ]
    return messages


@pytest.mark.parametrize("fold", [fold_stale_scenes, fold_stale_knowledge])
def test_neither_other_fold_touches_a_loaded_skill(fold) -> None:
    messages = conversation(
        loaded("knowledge_base", SKILLS["knowledge_base"]),
        loaded("content_map", SKILLS["content_map"]),
    )
    folded = fold(messages, keep=0)
    assert folded[2] is messages[2]
    assert folded[8] is messages[8]


def test_the_skill_fold_touches_no_scene_view() -> None:
    messages = conversation(
        loaded("knowledge_base", SKILLS["knowledge_base"]),
        loaded("content_map", SKILLS["content_map"]),
    )
    folded = fold_stale_skills(messages, keep=0)
    assert folded[4] is messages[4]
    assert folded[6] is messages[6]


def test_an_older_skill_folds_once_a_newer_one_is_loaded() -> None:
    messages = conversation(
        loaded("knowledge_base", SKILLS["knowledge_base"]),
        loaded("content_map", SKILLS["content_map"]),
    )
    folded = fold_stale_skills(messages)
    assert SKILLS["knowledge_base"] not in folded[2].content
    assert 'load_skill("knowledge_base")' in folded[2].content
    assert folded[8] is messages[8]


def test_a_single_loaded_skill_survives_any_number_of_turns() -> None:
    messages = conversation(loaded("knowledge_base", SKILLS["knowledge_base"]))
    assert fold_stale_skills(messages)[2] is messages[2]


def test_no_v19_skill_carries_a_marker_another_fold_looks_for() -> None:
    """The scene and knowledge folds replace only a full marked span: a start
    marker with its number or id, the body, and the end marker. A skill body that
    quoted a whole span would lose it on the next turn; naming a marker in prose,
    as `<<scene view N>>` does, is not a span and is left alone."""
    for name in skill_names("v19"):
        messages = conversation(loaded(name, load_skill(name, "v19").body))
        folded = fold_stale_knowledge(fold_stale_scenes(messages, keep=0), keep=0)
        assert folded[2] is messages[2], name


def test_every_real_v19_skill_folds_whole() -> None:
    """A real body must not end its own span early, or part of it would survive the fold."""
    for name in skill_names("v19"):
        body = load_skill(name, "v19").body
        messages = conversation(loaded(name, body))
        folded = fold_stale_skills(messages, keep=0)[2].content
        assert folded.startswith(f"[skill {name} folded"), name
        assert body.strip().splitlines()[-1] not in folded, name


def test_naming_a_marker_in_prose_is_not_folded() -> None:
    body = (
        f"A view is marked `{SCENE_VIEW_START_PREFIX}N{SCENE_VIEW_START_SUFFIX}`, and "
        f"neighbours `{NEIGHBOUR_BLOCK_START_PREFIX}id>>`."
    )
    messages = conversation(loaded("knowledge_base", body))
    assert fold_stale_knowledge(fold_stale_scenes(messages, keep=0), keep=0)[2] is messages[2]


# --- folding, through the middleware list ------------------------------------


def arch_with(**update):
    return default_resolved_arch().model_copy(update=update)


def test_the_skill_fold_is_wired_only_when_skills_load_on_demand() -> None:
    assert "fold_stale_skills" in middleware_names_for(arch_with(skills="on_demand"))
    assert "fold_stale_skills" not in middleware_names_for(arch_with(skills="off"))


def test_the_knob_off_leaves_skills_unfolded() -> None:
    names = middleware_names_for(arch_with(skills="on_demand", fold_stale_skills=False))
    assert "fold_stale_skills" not in names
    # The other two folds do not depend on it.
    assert "fold_scene_views" in names
    assert "fold_knowledge_neighbours" in names


def test_with_skills_off_the_knob_changes_nothing() -> None:
    """With `off` no tool result carries a skill, so the knob has nothing to switch."""
    on = middleware_names_for(arch_with(skills="off", fold_stale_skills=True))
    off = middleware_names_for(arch_with(skills="off", fold_stale_skills=False))
    assert on == off


# --- the phase gate -----------------------------------------------------------


def test_the_phase_gate_never_refuses_load_skill() -> None:
    """A skill is needed at the moment it applies, and that can be any phase.

    `knowledge_base` is read in UPDATE_MEMORY and `held_state` in ACT, so the gate
    must let `load_skill` through wherever the run stands. It is in neither of
    `phase.py`'s tables, which is what makes it pass, and it is not named in
    `phase_directive`, which a `skills=off` run also reads.
    """
    from app.agents.qa.arch import PhaseCycleMode
    from app.agents.qa.tools.phase import (
        ALWAYS_ALLOWED,
        _TOOL_PHASE,
        RunPhase,
        build_phase_cycle,
    )

    assert "load_skill" not in ALWAYS_ALLOWED
    assert "load_skill" not in _TOOL_PHASE
    cycle = build_phase_cycle(PhaseCycleMode.full)
    for phase in RunPhase:
        cycle.phase = phase
        assert cycle.refusal_for("load_skill") is None
    assert cycle.refusals == 0
