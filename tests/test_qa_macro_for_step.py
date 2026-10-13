"""다음 step 에 맞는 등록된 macro 를 `report_step` 의 답에 이름으로 대는지 재는 테스트.

맞춤은 순수 함수(`macros_for_step`)로 재고, 답에 붙는 것은 실제 tool 로 잰다. 문구 전체는 다른
곳에서 바뀌므로 macro 이름과 `run_macro` 를 대는지만 본다. 마지막으로 `UPDATE_MEMORY` 와
`REVIEW_DRAFT` 에서 `run_macro` 를 거절하는 문구가 "다음 step 의 첫 동작으로 하라" 고 덧붙이는지
본다.
"""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.macro.book import RegisteredMacro
from app.agents.qa.macro.for_step import (
    StepMacro,
    first_body_comment,
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


def registered(name: str, comment: str | None) -> RegisteredMacro:
    body = f"    # {comment}\n" if comment else ""
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
    assert found == [StepMacro("replay_step_4", "Step 4: mine")]


def test_nothing_matching_names_nothing() -> None:
    assert macros_for_step(5, {}, [scene_macro("replay_step_4")]) == []
    assert render_step_macro_hint(5, []) == ""


def test_the_first_comment_of_the_entry_body_is_the_summary() -> None:
    source = "# header\ndef a() -> None:\n    # Step 3: x — 2 actions\n    # second\n    click()\n"
    assert first_body_comment(source, "a") == "Step 3: x — 2 actions"
    assert first_body_comment("def a() -> None:\n    click()\n", "a") is None


def test_the_hint_clips_a_long_summary_and_names_the_next_action() -> None:
    hint = render_step_macro_hint(4, [StepMacro("replay_step_4", "Step 4: " + "가" * 300)])
    assert "`replay_step_4`" in hint
    assert "`run_macro` as the first action of step 4" in hint
    assert "…" in hint and len(hint) < 400


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
        LLMModel.gpt_6_luna,
    )
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    return state, tools


async def report(tools, step: int, passed: bool = True) -> str:
    return await tools["report_step"].ainvoke(
        {"step": step, "passed": passed, "message": "m", "thought": "t"}
    )


def test_reporting_step_1_names_the_macro_of_step_2() -> None:
    async def scenario() -> None:
        _state, tools = make("on")
        answer = await report(tools, 1)
        assert "continue with step 2" in answer
        assert "`replay_step_2`" in answer
        assert "Step 2: 오프닝을 넘긴다" in answer
        assert "`run_macro` as the first action of step 2" in answer

    asyncio.run(scenario())


def test_a_failed_step_still_names_the_macro_of_the_next_one() -> None:
    async def scenario() -> None:
        _state, tools = make("on")
        answer = await report(tools, 1, passed=False)
        assert "`replay_step_2`" in answer

    asyncio.run(scenario())


def test_a_step_whose_next_has_no_macro_gets_no_sentence() -> None:
    async def scenario() -> None:
        _state, tools = make("on")
        answer = await report(tools, 2)
        assert "continue with step 3" in answer
        assert "registered macro" not in answer

    asyncio.run(scenario())


def test_the_last_step_answer_is_unchanged() -> None:
    async def scenario() -> None:
        state, tools = make("on")
        state.total_steps = 2
        await report(tools, 1)
        answer = await report(tools, 2)
        assert "last step" in answer
        assert "registered macro" not in answer

    asyncio.run(scenario())


def test_macros_off_adds_nothing() -> None:
    async def scenario() -> None:
        _state, tools = make("off")
        answer = await report(tools, 1)
        assert "continue with step 2" in answer
        assert "replay_step_2" not in answer
        assert "run_macro" not in answer

    asyncio.run(scenario())


# --- running a macro out of phase ---------------------------------------------


def _into(cycle: PhaseCycle, draft: bool) -> None:
    for name in ("observe_scene", "click"):
        cycle.advance(name)
    if draft:
        cycle.expect_draft_review()
    cycle.advance("report_step")


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
