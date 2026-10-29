"""macro 조회와 phase gate를 유지하고 TC 판정이 다음 참고 step을 강제하지 않는지 확인한다."""

import asyncio
from tests.qa_attempts import configure_attempts, start_case, report_case

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.macro.book import RegisteredMacro
from app.agents.qa.macro.for_step import (
    StepMacro,
    macros_for_step,
    render_step_macro_hint,
)
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import (
    MACRO_AFTER_MEMORY_SENTENCE,
    PhaseCycle,
    RunPhase,
    build_phase_cycle,
)
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.scene_context import SceneContext, SceneMacro


def scene_macro(name: str, summary: str | None = None) -> SceneMacro:
    return SceneMacro(name=name, summary=summary)


def registered(name: str, docstring: str | None) -> RegisteredMacro:
    body = f'    """{docstring}"""\n' if docstring else ""
    source = f"def {name}() -> None:\n{body}    press_key(\"Space\", 0.1)\n"
    return RegisteredMacro(definition=macro_definition_from_source(name, source))


def names(found: list[StepMacro]) -> list[str]:
    return [one.name for one in found]


# --- matcher ------------------------------------------------------------------


def test_a_summary_that_starts_with_the_step_number_matches() -> None:
    found = macros_for_step(
        2, {}, [scene_macro("opening", "Step 2: 오프닝을 넘긴다 — 5 actions, drafted."), scene_macro("x")]
    )
    assert names(found) == ["opening"]


def test_a_replay_name_matches_with_and_without_a_suffix() -> None:
    found = macros_for_step(
        2,
        {},
        [scene_macro("replay_step_2"), scene_macro("replay_step_2_3"), scene_macro("replay_step_20")],
    )
    assert names(found) == ["replay_step_2", "replay_step_2_3"]


def test_step_1_does_not_match_step_13() -> None:
    found = macros_for_step(
        1,
        {},
        [
            scene_macro("replay_step_13", "Step 13: 상점을 연다 — 2 actions"),
            scene_macro("replay_step_1_other"),
            scene_macro("replay_step_10_2"),
        ],
    )
    assert found == []


def test_this_runs_registrations_come_first_and_at_most_two_are_named() -> None:
    mine = registered("replay_step_4", "Step 4: 직접 고친 것 — 3 actions")
    found = macros_for_step(
        4,
        {"replay_step_4": mine},
        [scene_macro("older_a", "Step 4: a"), scene_macro("older_b", "Step 4: b")],
    )
    assert names(found) == ["replay_step_4", "older_a"]
    assert found[0].summary == "Step 4: 직접 고친 것 — 3 actions"


def test_a_name_in_both_sources_is_counted_once() -> None:
    mine = registered("replay_step_4", "Step 4: mine")
    found = macros_for_step(4, {"replay_step_4": mine}, [scene_macro("replay_step_4", "Step 4: old")])
    assert names(found) == ["replay_step_4"]
    assert found[0].summary == "Step 4: mine"


def test_nothing_matching_names_nothing() -> None:
    assert macros_for_step(5, {}, [scene_macro("replay_step_4")]) == []
    assert render_step_macro_hint(5, []) == ""


def test_the_entry_docstring_is_the_summary_and_a_comment_is_not() -> None:
    """summary 는 진입점 `def` 의 docstring 이다. 몸통 첫 `#` 줄은 더는 summary 가 아니다."""
    with_docstring = registered("a", "Step 3: x — 2 actions")
    found = macros_for_step(3, {"a": with_docstring}, [])
    assert [(one.name, one.summary) for one in found] == [("a", "Step 3: x — 2 actions")]

    commented = RegisteredMacro(
        definition=macro_definition_from_source(
            "b", 'def b() -> None:\n    # Step 3: x\n    press_key("Space", 0.1)\n'
        )
    )
    assert macros_for_step(3, {"b": commented}, []) == []


def test_the_hint_clips_a_long_summary_and_names_the_next_action() -> None:
    hint = render_step_macro_hint(4, [StepMacro("replay_step_4", "Step 4: " + "가" * 300)])
    assert "def replay_step_4()  [no screen yet]" in hint
    assert "`run_macro` as the first action of step 4" in hint
    assert "…" in hint and len(hint) < 400


def test_the_hint_draws_each_macro_as_a_def_with_its_docstring_under_it() -> None:
    mine = RegisteredMacro(
        definition=macro_definition_from_source(
            "replay_step_2",
            'def replay_step_2(card: object) -> None:\n'
            '    """Step 2: deal a card.\n\n    Second paragraph is not drawn."""\n'
            "    click(card)\n",
        ),
        screens=("41",),
    )
    older = SceneMacro.model_validate(
        {
            "name": "replay_step_2_2",
            "parameters": [{"name": "count", "type": "int"}],
            "summary": "Step 2: older one",
            "screens": [{"screenId": "7", "sceneName": "TurnBattleScene"}],
        }
    )
    found = macros_for_step(2, {"replay_step_2": mine}, [older])
    hint = render_step_macro_hint(2, found)

    assert hint == (
        "\n\nStep 2 has registered macros:\n"
        "  def replay_step_2(card: object)  [screen 41]\n"
        '      """Step 2: deal a card."""\n'
        "  def replay_step_2_2(count: int)  [TurnBattleScene]\n"
        '      """Step 2: older one"""\n'
        "Run one of them with `run_macro` as the first action of step 2; act by hand "
        "only if it fails or does not cover the step."
    )


# --- through the real tools ---------------------------------------------------


def make(macros: str):
    async def send(frame: dict) -> None:
        pass

    channel = QaRunChannel(qa_try_id=9, send=send, action_timeout=0.05, write_timeout=0.05)
    channel.scene.scene_context = SceneContext(
        macros=[scene_macro("replay_step_2", "Step 2: 오프닝을 넘긴다 — 13 actions, drafted.")],
        macros_total=1,
    )
    state = QaRunState(total_steps=3)
    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.off, macros=macros),
        "openai/gpt-6-luna",
    )
    configure_attempts(channel, state)
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    return state, tools


def test_tc_report_does_not_choose_the_next_reference_step():
    async def run():
        state, tools = make("on")
        await start_case(tools, 1)
        text = await report_case(tools, 1)
        assert "continue with step" not in text
        assert "last step" not in text
        assert "replay_step_2" not in text
        assert state.case_attempt_results[0].case_id == 1
        assert not state.finished
    asyncio.run(run())


# --- running a macro out of phase ---------------------------------------------


def _into(cycle: PhaseCycle, draft: bool) -> None:
    for name in ("observe_scene", "click"):
        cycle.advance(name)
    if draft:
        cycle.expect_draft_review()
    cycle.advance("report_test_case")


def test_run_macro_refused_in_update_memory_says_to_run_it_next_step() -> None:
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    _into(cycle, draft=False)
    assert cycle.phase is RunPhase.update_memory

    for name in ("run_macro", "resume_macro"):
        refusal = cycle.refusal_for(name)
        assert refusal is not None
        assert "UPDATE_MEMORY ends when you call one of:" in refusal
        assert refusal.endswith(MACRO_AFTER_MEMORY_SENTENCE)
        assert "first action of the next step" in refusal


def test_run_macro_refused_in_review_draft_says_the_same() -> None:
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    _into(cycle, draft=True)
    assert cycle.phase is RunPhase.review_draft

    refusal = cycle.refusal_for("run_macro")
    assert refusal is not None
    assert "REVIEW_DRAFT ends when you call one of:" in refusal
    assert "first action of the next step" in refusal


def test_other_tools_refused_in_update_memory_do_not_get_the_sentence() -> None:
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    _into(cycle, draft=False)
    refusal = cycle.refusal_for("click")
    assert refusal is not None
    assert "first action of the next step" not in refusal
