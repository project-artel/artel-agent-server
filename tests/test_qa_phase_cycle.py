"""The phase cycle as a state machine, and the two tools that only exist with it.

Every test here names a `phase_cycle` value rather than taking the default. The
file is about what each rung does, and a test that read the default would change
meaning the next time the default moves — which it has: `lite` is what a run
that names nothing now gets. What the default is belongs in one place, and that
place is `tests/test_qa_arch.py`.

`off` still has to cost exactly the round trips it cost before this axis existed.
ARTEL-667 sent the run back once from `finish_run` and broke eighteen tests that
close a run; this axis sends it back at every step, so
`test_the_off_rung_is_the_run_that_existed_before_the_axis` guards that boundary
from the other side.
"""

import asyncio
import re

import pytest

from app.agents.qa.arch import (
    PhaseCycleMode,
    QaArchSpec,
    VisionMode,
    resolve_arch,
    structure_of,
)
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import (
    ALWAYS_ALLOWED,
    MAX_CONSECUTIVE_REFUSALS,
    PhaseCycle,
    RunPhase,
    _TOOL_PHASE,
    build_phase_cycle,
)
from app.llm.models import LLMModel
from app.prompts import load_prompt
from app.qa.channel import QaRunChannel


def arch_for(mode: PhaseCycleMode):
    return resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=mode), LLMModel.gpt_6_luna
    )


def make(mode: PhaseCycleMode, total_steps: int = 2, timeout: float = 0.05):
    """A channel, a run state and the tool set one `phase_cycle` value builds."""
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout, write_timeout=timeout)
    state = QaRunState(total_steps=total_steps)
    tools = {tool.name: tool for tool in build_tools(channel, state, arch_for(mode))}
    return channel, state, tools, sent


def standing_on(channel: QaRunChannel, scene: str = "Lobby") -> None:
    channel.on_game_state(
        {"type": "GAME_STATE", "payload": {"scene": scene, "interactables": [], "observables": {}}}
    )


# --- the table ----------------------------------------------------------------


def test_every_tool_the_default_run_offers_has_a_phase_or_is_always_allowed() -> None:
    """The one failure `phase.py` cannot recover from at run time.

    An unmapped tool passes the gate, which is deliberate — a run trapped by a
    name somebody forgot is worse than a tool that briefly belongs to no phase.
    The cost of that choice is that the omission is silent, so it is caught here
    instead: a tool added to `build_tools` and to neither `_TOOL_PHASE` nor
    `ALWAYS_ALLOWED` fails this test rather than quietly opting itself out.
    """
    names, _middleware, _print = structure_of(arch_for(PhaseCycleMode.full))
    unplaced = [
        name for name in names if name not in _TOOL_PHASE and name not in ALWAYS_ALLOWED
    ]
    assert unplaced == []


def test_the_directive_names_the_same_tools_the_table_does() -> None:
    """The prompt and `_TOOL_PHASE` are two copies of one rule, so they drift.

    The gate refuses from the table; the model plans from the text. A tool the
    table moved and the text still lists somewhere else costs a refusal every
    time the model follows the instructions it was given, and nothing in a run
    says which of the two was wrong. This is cheaper than finding out in a
    pilot.

    Only the two lists a run is steered by are checked — what answers
    UPDATE_MEMORY, and what is exempt from the order — because those are the
    ones a wrong answer traps the run on. And nothing outside the default tool
    set may be named at all: a name the model cannot call is worse than no
    advice.
    """
    for version, macros in (("v18", "off"), ("v19", "off"), ("v19", "on")):
        _the_directive_names_what_this_structure_offers(version, macros)


def _the_directive_names_what_this_structure_offers(version: str, macros: str) -> None:
    """한 구조가 읽는 지시문과 그 구조가 가진 tool 을 맞댄다.

    표는 모든 구조에 하나지만 tool 목록은 구조마다 다르다. `macros=on` 이면 macro tool 넷이
    표에 들어 있고 지시문에 macro 문단이 붙는다. `macros=off` 에서는 둘 다 없다 — 그래서 이
    구조가 실제로 가진 이름만 요구한다. 지시문은 runner 가 조립하는 그대로 받는다.
    """
    from app.agents.qa.runner import phase_directive_for

    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=PhaseCycleMode.full, macros=macros),
        LLMModel.gpt_6_luna,
    )
    body = phase_directive_for(arch, version)
    tools, _middleware, _print = structure_of(arch)
    tools = set(tools)
    where = f"{version}, macros={macros}"

    for name in ALWAYS_ALLOWED & tools:
        assert f"`{name}`" in body, f"{name} is exempt from the order and unsaid ({where})"

    for name, at in _TOOL_PHASE.items():
        if at is RunPhase.update_memory and name in tools:
            assert f"`{name}`" in body, f"{name} answers UPDATE_MEMORY and is unsaid ({where})"

    # Backticks in this file mark tool names and argument names alike, and the
    # only argument it has reason to mention is the one it refuses when empty.
    arguments = {"reason"}
    named = set(re.findall(r"`([a-z_]+)`", body)) - arguments
    assert named <= tools, f"the directive names tools that do not exist: {named - tools}"


def test_the_tools_that_pause_the_game_do_not_end_the_act_phase() -> None:
    """Stopping the clock to read the screen is observation, not an action.

    They are in `ALWAYS_ALLOWED` for that reason, and out of `_TOOL_PHASE` for the
    same one: a run that paused the game to look at it has not performed the
    step's action, and advancing past `ACT` there would hand `VERIFY` a screen
    nothing was done to.
    """
    for name in ("pause_game_time", "resume_game_time"):
        assert name in ALWAYS_ALLOWED
        assert name not in _TOOL_PHASE


# --- transitions --------------------------------------------------------------


def test_lite_walks_four_phases_and_comes_back_to_observe() -> None:
    """One lap, with the three repeating phases doing the repeating.

    Only `report_step` moves the run on by being called — it is the one tool in
    the lap whose phase ends on it. The other three phases are left by calling
    the tool of a later one, so the phase after an `observe_scene` or a `click`
    is the phase it was already in.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)

    assert cycle.phase is RunPhase.observe
    for name, expected in (
        # OBSERVE does not end on a look — the run may look as many times as it
        # needs to.
        ("observe_scene", RunPhase.observe),
        ("capture_screen", RunPhase.observe),
        # ACT opens on the first action and does not end on it either.
        ("click", RunPhase.act),
        ("click", RunPhase.act),
        # `report_step` closes ACT and VERIFY in one call.
        ("report_step", RunPhase.update_memory),
        # And UPDATE_MEMORY stays open for a second thing to write down.
        ("skip_memory_update", RunPhase.update_memory),
        # The next lap starts when the run looks at the screen again.
        ("observe_scene", RunPhase.observe),
    ):
        assert cycle.refusal_for(name) is None
        cycle.advance(name)
        assert cycle.phase is expected

    assert cycle.refusals == 0


def test_act_stays_open_for_as_many_actions_as_the_step_needs() -> None:
    """One scenario step is not one action, so ACT must not end on the first one.

    L1 step 3 is "advance the opening story to the end" and takes a key press per
    line of dialogue. With ACT ending on the first press, the second one is
    refused: in the 2026-09-18 pilot, 51 of 147 refusals were `press_key` called
    in VERIFY, which is a third of every refusal the run made.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    cycle.advance("observe_scene")

    for _ in range(5):
        assert cycle.refusal_for("press_key") is None
        cycle.advance("press_key")
        assert cycle.phase is RunPhase.act

    assert cycle.refusals == 0
    assert cycle.forced_passes == 0


def test_observe_stays_open_for_as_many_looks_as_the_step_needs() -> None:
    """Reading is not one call either, and the knowledge tools are the reason.

    `search_knowledge` answers with rows that `expand_knowledge` opens up, a
    screen capture is worth taking again after an animation, and `inspect_object`
    asks about one element at a time. Ending OBSERVE on the first of those
    refuses the second — and `observe_scene` alone hid it, because that one is in
    `ALWAYS_ALLOWED` and never gets refused whatever the phase says.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)

    for name in (
        "search_knowledge",
        "expand_knowledge",
        "capture_screen",
        "inspect_object",
        "list_scene_capabilities",
        "capture_screen",
    ):
        assert cycle.refusal_for(name) is None, name
        cycle.advance(name)
        assert cycle.phase is RunPhase.observe

    assert cycle.refusals == 0
    assert cycle.forced_passes == 0


def test_update_memory_stays_open_for_a_correction_that_takes_two_calls() -> None:
    """Correcting an entry is `forget_knowledge` and then `record_knowledge`.

    They are one act with two calls, and a phase that closed on the first would
    refuse the half that puts the right answer back — leaving the run worse off
    than if it had written nothing.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    cycle.advance("report_step")
    assert cycle.phase is RunPhase.update_memory

    for name in ("forget_knowledge", "record_knowledge", "record_capability_verdict"):
        assert cycle.refusal_for(name) is None, name
        cycle.advance(name)
        assert cycle.phase is RunPhase.update_memory

    assert cycle.refusals == 0


def test_each_step_owes_its_own_memory_update() -> None:
    """The lap that a repeating last phase could otherwise swallow.

    UPDATE_MEMORY is left by calling a later phase's tool, and the phase after
    VERIFY is UPDATE_MEMORY again — so a run sitting there can report the next
    step's verdict without ever passing through anything else. If what it wrote
    down for step 1 still counted, one record at the top of the run would buy
    every remaining step a free pass, which is the whole thing this rung exists
    to prevent.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    cycle.advance("report_step")
    cycle.advance("record_knowledge")
    assert cycle.refusal_for("capture_screen") is None

    # Step 2's verdict, reported without leaving UPDATE_MEMORY.
    assert cycle.refusal_for("report_step") is None
    cycle.advance("report_step")
    assert cycle.phase is RunPhase.update_memory

    # Step 1's record does not pay for step 2.
    assert "UPDATE_MEMORY" in cycle.refusal_for("capture_screen")


def test_an_action_satisfies_observe_on_its_way_past() -> None:
    """The base prompt already says an action returns the scene it produced.

    `system.md`: "Each of those returns the outcome AND the scene it produced...
    you usually do not need a separate observation afterwards." The gate did not
    know that and demanded `observe_scene` at the top of every lap. In the
    2026-09-18 pilot that was 97 of 137 refusals, 50 of them an action tool
    called from OBSERVE.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    assert cycle.phase is RunPhase.observe

    assert cycle.refusal_for("press_key") is None
    cycle.advance("press_key")
    assert cycle.phase is RunPhase.act
    assert cycle.refusals == 0


def test_a_step_that_only_looks_needs_no_action() -> None:
    """L1 step 1 is "observe the title screen". Demanding an action invents one."""
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    cycle.advance("observe_scene")

    assert cycle.refusal_for("report_step") is None
    cycle.advance("report_step")
    assert cycle.phase is RunPhase.update_memory


def test_decide_is_not_skippable_because_it_is_the_whole_full_arm() -> None:
    """`full` still cannot reach ACT without writing the decision down.

    The refusal names OBSERVE rather than DECIDE — it reports the phase the run
    is in, and DECIDE is what makes the jump unreachable from there. Following
    the message still works: `observe_scene` lands in DECIDE, and DECIDE only
    opens on `decide_next_action`.
    """
    cycle = build_phase_cycle(PhaseCycleMode.full)

    assert "DECIDE" in cycle.refusal_for("press_key")
    assert cycle.phase is RunPhase.observe

    # Looking again does not get any closer to ACT, and says so the same way.
    cycle.advance("observe_scene")
    assert cycle.phase is RunPhase.observe
    assert "DECIDE" in cycle.refusal_for("press_key")

    # Writing the decision down is the only way through, and it lands in ACT.
    assert cycle.refusal_for("decide_next_action") is None
    cycle.advance("decide_next_action")
    assert cycle.phase is RunPhase.act


def test_update_memory_is_still_the_one_phase_that_cannot_be_skipped() -> None:
    """Making ACT repeatable must not open a way around the phase that matters.

    The whole axis exists to make UPDATE_MEMORY happen; every other phase is
    scaffolding around it.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    cycle.advance("observe_scene")
    cycle.advance("press_key")
    cycle.advance("report_step")
    assert cycle.phase is RunPhase.update_memory

    assert "UPDATE_MEMORY" in cycle.refusal_for("press_key")


def test_full_puts_decide_between_observing_and_acting() -> None:
    cycle = build_phase_cycle(PhaseCycleMode.full)

    cycle.advance("observe_scene")
    # An action tool here is the whole point of the `full` arm: the decision has
    # to be written down before it is carried out, or there is nothing to read
    # back.
    assert "DECIDE" in cycle.refusal_for("click")
    cycle.advance("decide_next_action")
    assert cycle.phase is RunPhase.act

    # `lite` has no DECIDE at all, so the same call goes straight through.
    lite = build_phase_cycle(PhaseCycleMode.lite)
    assert lite.refusal_for("click") is None


def test_off_and_in_verdict_build_no_state_machine() -> None:
    """The two cheap rungs gate nothing, so nothing can be refused in them."""
    assert build_phase_cycle(PhaseCycleMode.off) is None
    assert build_phase_cycle(PhaseCycleMode.remember_in_verdict) is None


def test_looking_again_mid_action_does_not_send_the_phase_backwards() -> None:
    """`observe_scene` is both always allowed and what ends `OBSERVE`.

    A run in `ACT` that looks at the screen again — a loading spinner, an
    animation, a countdown — must not be dropped back to `OBSERVE`, or it can
    never get an action in at all.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    cycle.advance("press_key")
    assert cycle.phase is RunPhase.act

    assert cycle.refusal_for("observe_scene") is None
    cycle.advance("observe_scene")
    assert cycle.phase is RunPhase.act


@pytest.mark.parametrize("name", sorted(ALWAYS_ALLOWED))
def test_the_always_allowed_tools_are_allowed_in_every_phase(name) -> None:
    """Without this list the run is trapped, so it is checked against every phase
    rather than against the one it was written for."""
    for phase in RunPhase:
        cycle = PhaseCycle((phase,))
        assert cycle.refusal_for(name) is None


# --- refusals -----------------------------------------------------------------


def test_an_out_of_phase_call_is_refused_and_counted() -> None:
    """The refusal says which phase the run is in and what ends it.

    A refusal the model cannot act on costs a round trip and buys nothing, so the
    text names the tools that would move the run on rather than only saying no.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)
    refusal = cycle.refusal_for("record_knowledge")

    assert "OBSERVE" in refusal and "UPDATE_MEMORY" in refusal
    # The tool it names is the one that unblocks the jump, which is not the same
    # as one that ends the phase the run is sitting in. OBSERVE is skippable, so
    # nothing is owed there; what stands between here and UPDATE_MEMORY is
    # VERIFY, and `report_step` is how that is answered. Naming `observe_scene`
    # instead would be advice the model can follow and still be refused.
    assert "VERIFY ends when you call one of: `report_step`" in refusal
    assert cycle.refusals == 1
    assert cycle.phase is RunPhase.observe


def test_the_run_is_let_through_after_two_refusals_in_a_row() -> None:
    """The cap the plan left open, and what it costs either way.

    A refused model that calls the same tool again burns two round trips for
    nothing. Two refusals is the bound: the first tells the model what the phase
    wants, and a model that has not acted on that by the second call will not act
    on a third. The call is then let through AND the phase moves to where the
    model actually is — letting it through while leaving the phase behind only
    refuses the next call as well.
    """
    cycle = build_phase_cycle(PhaseCycleMode.lite)

    for _ in range(MAX_CONSECUTIVE_REFUSALS):
        assert cycle.refusal_for("record_knowledge") is not None

    assert cycle.refusal_for("record_knowledge") is None
    assert cycle.forced_passes == 1
    assert cycle.refusals == MAX_CONSECUTIVE_REFUSALS
    assert cycle.phase is RunPhase.update_memory


def test_an_in_phase_call_clears_the_refusal_streak() -> None:
    """Consecutive means consecutive: a model that corrected itself starts over."""
    cycle = build_phase_cycle(PhaseCycleMode.lite)

    assert cycle.refusal_for("record_knowledge") is not None
    assert cycle.refusal_for("observe_scene") is None
    cycle.advance("observe_scene")
    assert cycle.refusal_for("record_knowledge") is not None

    assert cycle.forced_passes == 0
    assert cycle.refusals == 2


def test_a_refused_tool_does_not_run() -> None:
    """The refusal is a tool result, so nothing the tool would have done happened."""

    async def run() -> None:
        channel, state, tools, sent = make(PhaseCycleMode.lite)
        standing_on(channel)

        # UPDATE_MEMORY 로 보낸다. 거기서 `report_step` 은 뒤로 가는 호출이라 거절된다 —
        # OBSERVE 와 ACT 는 건너뛸 수 있지만 UPDATE_MEMORY 는 아니다.
        state.phase_cycle.advance("report_step")
        assert state.phase_cycle.phase is RunPhase.update_memory
        before = len(sent)

        result = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "saw it",
                "thought": "판정한다",
                "learned": "",
            }
        )

        assert "UPDATE_MEMORY" in result
        assert state.step_results == []
        assert len(sent) == before
        assert state.phase_cycle.refusals == 1

    asyncio.run(run())


# --- the two tools ------------------------------------------------------------


def test_skipping_the_memory_update_needs_a_reason() -> None:
    """Silence is what this tool exists to replace, so a blank answer is refused."""

    async def run() -> None:
        _channel, state, tools, _sent = make(PhaseCycleMode.lite)
        state.phase_cycle.phase = RunPhase.update_memory

        refused = await tools["skip_memory_update"].ainvoke({"step": 1, "reason": "   "})
        assert "`reason` is empty" in refused

        accepted = await tools["skip_memory_update"].ainvoke(
            {"step": 1, "reason": "the button did what its label says"}
        )
        assert "Noted" in accepted

    asyncio.run(run())


def test_an_empty_reason_does_not_buy_a_way_past_update_memory() -> None:
    """The hole `PhaseCycle.hold` closes, stated as the behaviour rather than the call.

    The gate only knows that a tool ran, not what it answered. Without `hold`, a
    `skip_memory_update` with an empty `reason` would be refused by the tool and
    still counted by the gate as `UPDATE_MEMORY` having happened — which turns the
    question this whole rung exists to ask into one blank argument away.
    """

    async def run() -> None:
        _channel, state, tools, _sent = make(PhaseCycleMode.lite)
        state.phase_cycle.phase = RunPhase.update_memory

        # UPDATE_MEMORY repeats, so the phase alone cannot say whether anything
        # was written — it reads `UPDATE_MEMORY` either way. What separates the
        # two is whether the run is allowed to leave, which is what the gate
        # actually enforces.
        await tools["skip_memory_update"].ainvoke({"step": 1, "reason": ""})
        assert state.phase_cycle.phase is RunPhase.update_memory
        assert state.phase_cycle.refusal_for("observe_scene") is None  # always allowed
        assert state.phase_cycle.refusal_for("capture_screen") is not None

        await tools["skip_memory_update"].ainvoke({"step": 1, "reason": "nothing new here"})
        assert state.phase_cycle.phase is RunPhase.update_memory
        assert state.phase_cycle.refusal_for("capture_screen") is None

    asyncio.run(run())


def test_an_omitted_learned_does_not_buy_a_way_past_verify() -> None:
    """Same hole, at `report_step`. No verdict was recorded, so `VERIFY` is not done."""

    async def run() -> None:
        channel, state, tools, _sent = make(PhaseCycleMode.lite)
        standing_on(channel)
        state.phase_cycle.phase = RunPhase.verify

        await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "saw it", "thought": "판정한다"}
        )

        assert state.step_results == []
        assert state.phase_cycle.phase is RunPhase.verify

    asyncio.run(run())


def test_deciding_answers_with_one_line_and_touches_nothing() -> None:
    """`decide_next_action` costs a model call and must not cost a game one too."""

    async def run() -> None:
        _channel, state, tools, sent = make(PhaseCycleMode.full)
        state.phase_cycle.phase = RunPhase.decide

        result = await tools["decide_next_action"].ainvoke(
            {
                "step": 3,
                "plan": "click the Start button",
                "expected": "the lobby closes and the map opens",
                "thought": "여기서 시작한다",
            }
        )

        assert "step 3" in result
        assert sent == []

    asyncio.run(run())


def test_the_new_tools_appear_only_at_the_rung_that_pays_for_them() -> None:
    off, _m, _f = structure_of(arch_for(PhaseCycleMode.off))
    in_verdict, _m, _f = structure_of(arch_for(PhaseCycleMode.remember_in_verdict))
    lite, _m, _f = structure_of(arch_for(PhaseCycleMode.lite))
    full, _m, _f = structure_of(arch_for(PhaseCycleMode.full))

    assert off == in_verdict
    assert set(lite) - set(off) == {"skip_memory_update"}
    assert set(full) - set(off) == {"skip_memory_update", "decide_next_action"}


# --- macro 와 지식화 단계 -----------------------------------------------------------


def _lite(offered=None):
    from app.agents.qa.tools.phase import build_phase_cycle

    return build_phase_cycle(PhaseCycleMode.lite, offered)


def _into_update_memory(cycle) -> None:
    """한 step 을 손으로 하고 판정해서 `UPDATE_MEMORY` 에 앉힌다."""
    for name in ("observe_scene", "click", "report_step"):
        assert cycle.refusal_for(name) is None, name
        cycle.advance(name)
    assert cycle.phase is RunPhase.update_memory


def test_registering_a_macro_answers_update_memory() -> None:
    """등록한 macro 는 다음 런이 쓴다. knowledge 항목과 같은 "런을 넘어 남는 기록" 이다."""
    cycle = _lite()
    _into_update_memory(cycle)

    for name in ("read_macro", "write_macro", "edit_macro"):
        assert cycle.refusal_for(name) is None, name
        cycle.advance(name)
    # 초안은 지식이 아니다. 아직 이 step 의 지식화를 마치지 않았다.
    assert cycle.refusal_for("observe_scene") is None
    assert cycle.refusal_for("click") is not None

    assert cycle.refusal_for("register_macro") is None
    cycle.advance("register_macro")
    # 등록으로 이 step 의 지식화가 끝났다. 다음 step 의 ACT 로 갈 수 있다.
    assert cycle.refusal_for("run_macro") is None


def test_a_macro_registered_now_runs_from_the_next_step() -> None:
    cycle = _lite()
    _into_update_memory(cycle)
    cycle.advance("write_macro")
    cycle.advance("register_macro")

    assert cycle.refusal_for("run_macro") is None
    cycle.advance("run_macro")
    assert cycle.phase is RunPhase.act


def test_macro_drafts_can_be_written_and_read_in_any_phase() -> None:
    """초안은 런 밖에 아무것도 안 바꾼다. ACT 에서 실패한 macro 를 읽고 고칠 수 있어야 한다."""
    cycle = _lite()
    for name in ("observe_scene", "run_macro"):
        cycle.advance(name)
    assert cycle.phase is RunPhase.act

    for name in ("read_macro", "edit_macro", "write_macro"):
        assert cycle.refusal_for(name) is None, name
        cycle.advance(name)
    assert cycle.phase is RunPhase.act


def test_the_refusal_names_only_the_tools_this_run_has() -> None:
    """`macros=off` 런에 "`register_macro` 로 끝내라" 고 하면 없는 tool 을 찾는다."""
    offered = frozenset({"observe_scene", "click", "report_step", "record_knowledge", "skip_memory_update"})
    cycle = _lite(offered)
    _into_update_memory(cycle)

    refusal = cycle.refusal_for("click")

    assert refusal is not None
    assert "`record_knowledge`" in refusal
    assert "register_macro" not in refusal


@pytest.mark.parametrize(
    "version, macros, phase_cycle, applies",
    [
        ("v19", "on", PhaseCycleMode.lite, True),
        ("v19", "off", PhaseCycleMode.lite, False),
        ("v19", "on", PhaseCycleMode.off, False),
        ("v18", "on", PhaseCycleMode.lite, False),
    ],
)
def test_the_macro_paragraph_reaches_only_a_phased_run_with_macros_on_a_version_that_has_it(
    version: str, macros: str, phase_cycle: PhaseCycleMode, applies: bool
) -> None:
    """runner 가 싣는 것과 `run_config` 가 hash 하는 것이 같은 조건 하나에서 나온다."""
    from app.agents.qa.runner import phase_directive_for
    from app.qa.run_config import macro_memory_directive_applies

    arch = resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=phase_cycle, macros=macros),
        LLMModel.gpt_6_luna,
    )

    assert macro_memory_directive_applies(arch, version) is applies
    assert ("`register_macro` answers UPDATE_MEMORY" in phase_directive_for(arch, version)) is applies
