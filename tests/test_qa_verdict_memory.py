"""`report_step` 이 판정과 같은 숨에 무엇을 남길지 묻는 값, `remember_in_verdict`.

이 rung 은 모델 호출을 하나도 안 늘린다. 판정을 적는 turn 에 인자 둘이 더 붙을 뿐이고, 그
둘이 이 저장소가 세 번 문장으로 부탁하고 세 번 0 을 받은 질문이다 — v15 로 돈 27 런의 tool
호출 1,566 회 중 `record_capability_verdict` · `record_new_capability` ·
`list_scene_capabilities` 가 각각 0 회다.

여기 있는 테스트는 전부 `phase_cycle` 을 명시적으로 켠다. `off` 런이 왕복을 하나도 더 치르지
않는다는 반대편은 `tests/test_qa_arch.py` 의
`test_the_default_run_is_untouched_by_the_axis` 가 본다.
"""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import MAX_CONSECUTIVE_REFUSALS
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.scene_context import SceneCapability, SceneContext, SceneContextEntry


def arch_for(mode: PhaseCycleMode):
    return resolve_arch(
        QaArchSpec(vision=VisionMode.on, phase_cycle=mode), LLMModel.gpt_chat_latest
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


def with_content_map(
    channel: QaRunChannel, keys: list[str], scene: str = "Lobby", happenings: int = 0
) -> None:
    """Give the run a scene context entry holding these capability keys."""
    rows = [
        SceneCapability(
            capability_id=f"id-{key}",
            capability_key=key,
            summary=f"{key} does a thing",
            interaction="click",
        )
        for key in keys
    ]
    channel.scene.scene_context = SceneContext(
        scenes=[
            SceneContextEntry(
                scene_name=scene,
                known_to_content_map=True,
                capabilities=rows[: len(rows) - happenings],
                not_a_step_capabilities=rows[len(rows) - happenings :] if happenings else [],
            )
        ]
    )


def test_report_step_takes_the_two_arguments_only_when_asked_to() -> None:
    """The schema is the arm. `in_verdict` minus `off` is these two names."""

    def arguments(mode: PhaseCycleMode) -> set[str]:
        _channel, _state, tools, _sent = make(mode)
        return set(tools["report_step"].args)

    plain = arguments(PhaseCycleMode.off)
    assert plain == {"step", "passed", "message", "thought", "used_knowledge_ids"}
    for mode in (PhaseCycleMode.remember_in_verdict, PhaseCycleMode.lite, PhaseCycleMode.full):
        assert arguments(mode) - plain == {"capability_key", "learned"}


def test_an_omitted_learned_is_refused_and_records_nothing() -> None:
    """Omission is the failure this rung exists to answer, so it costs a call.

    Prompt v16 asked for the same thing in words and 27 runs made 1,566 tool
    calls with zero memory writes among them. An omission that is merely noted
    reproduces that; an omission that is sent back does not.
    """

    async def run() -> None:
        channel, state, tools, sent = make(PhaseCycleMode.remember_in_verdict)
        standing_on(channel)

        refused = await tools["report_step"].ainvoke(
            {"step": 1, "passed": True, "message": "saw it", "thought": "판정한다"}
        )

        assert "`learned`" in refused
        assert state.step_results == []
        assert sent == []

    asyncio.run(run())


def test_an_empty_learned_is_an_answer_and_the_verdict_is_recorded() -> None:
    """The empty string is how a run says this step left nothing behind.

    Separating it from an omitted argument is the whole mechanism: one is "I
    looked and there is none", the other is "I was not asked". A pilot that
    cannot tell them apart cannot read its own skip ratio.
    """

    async def run() -> None:
        channel, state, tools, _sent = make(PhaseCycleMode.remember_in_verdict)
        standing_on(channel)

        result = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "the lobby opened",
                "thought": "판정한다",
                "learned": "",
            }
        )

        assert "Recorded" in result
        assert [one.step for one in state.step_results] == [1]
        assert state.verdict_memory_refusals == 0

    asyncio.run(run())


def test_the_verdict_goes_through_after_two_omissions_in_a_row() -> None:
    """Same cap as the phase gate, and it says on the record that it was unanswered.

    A run stuck behind an argument it will not fill is a run that reports no
    verdicts at all, which is worse than a verdict with the question left open.
    """

    async def run() -> None:
        channel, state, tools, _sent = make(PhaseCycleMode.remember_in_verdict, total_steps=4)
        standing_on(channel)
        verdict = {"step": 1, "passed": True, "message": "saw it", "thought": "판정한다"}

        for _ in range(MAX_CONSECUTIVE_REFUSALS):
            assert "`learned`" in await tools["report_step"].ainvoke(dict(verdict))
        result = await tools["report_step"].ainvoke(dict(verdict))

        assert "unanswered" in result
        assert [one.step for one in state.step_results] == [1]

    asyncio.run(run())


def test_a_capability_key_this_run_was_never_shown_is_dropped() -> None:
    """The same check `used_knowledge_ids` makes, for the same reason.

    A key the model assembled still names a real row on the other side, and
    nothing over there can tell an invented one from a read one. The verdict
    itself stands — dropping the key is not a reason to lose the step.
    """

    async def run() -> None:
        channel, state, tools, _sent = make(PhaseCycleMode.remember_in_verdict)
        standing_on(channel)
        with_content_map(channel, ["Lobby.start#click"])

        result = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "the lobby opened",
                "thought": "판정한다",
                "learned": "",
                "capability_key": "Lobby.invented#click",
            }
        )

        assert "not a row this run has been shown" in result
        assert [one.step for one in state.step_results] == [1]

    asyncio.run(run())


def test_a_capability_key_printed_in_the_scene_context_block_is_accepted() -> None:
    """What the block printed is what the run was shown, cut lines included."""

    async def run() -> None:
        channel, _state, tools, _sent = make(PhaseCycleMode.remember_in_verdict)
        standing_on(channel)
        with_content_map(channel, ["Lobby.start#click"])

        result = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "the lobby opened",
                "thought": "판정한다",
                "learned": "",
                "capability_key": "Lobby.start#click",
            }
        )

        assert "not a row this run has been shown" not in result

    asyncio.run(run())


def test_a_key_cut_from_the_block_is_accepted_once_a_search_printed_it() -> None:
    """`list_scene_capabilities` is the other half of "shown".

    The block prints 8 of a scene's capability lines and 6 of its `happens`
    lines; on the measured build one scene holds 232. Rejecting everything the
    block had to cut would make the check refuse most of the map, so the run's
    own search results count too — and only the page a search actually printed,
    not the whole list it paged through.
    """

    async def run() -> None:
        channel, state, tools, _sent = make(PhaseCycleMode.remember_in_verdict)
        standing_on(channel)
        keys = [f"Lobby.row{index}#click" for index in range(12)]
        with_content_map(channel, keys)
        cut = keys[-1]

        before = await tools["report_step"].ainvoke(
            {
                "step": 1,
                "passed": True,
                "message": "saw it",
                "thought": "판정한다",
                "learned": "",
                "capability_key": cut,
            }
        )
        assert "not a row this run has been shown" in before

        await tools["list_scene_capabilities"].ainvoke(
            {"step": 2, "thought": "지도를 뒤진다", "contains": "row11"}
        )
        assert state.was_shown_capability_key(cut)

        after = await tools["report_step"].ainvoke(
            {
                "step": 2,
                "passed": True,
                "message": "saw it",
                "thought": "판정한다",
                "learned": "",
                "capability_key": cut,
            }
        )
        assert "not a row this run has been shown" not in after

    asyncio.run(run())
