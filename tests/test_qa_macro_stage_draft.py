"""stage draft: 한 `scene` visit 에서 부른 macro 를 엮은 초안 (ARTEL-1026)."""

import asyncio

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, VisionMode, resolve_arch
from app.agents.qa.macro.book import MacroBook
from app.agents.qa.macro.call_log import MacroCallLog
from app.agents.qa.macro.parser import macro_definition_from_source
from app.agents.qa.macro.stage_draft import (
    _rename_in_source,
    _snake,
    stage_draft_for_visit,
)
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.tools.phase import RunPhase
from app.llm.models import LLMModel
from app.qa.channel import QaRunChannel
from app.qa.pulse import PulseReading

CAST = (
    "def cast_pair(card: object) -> None:\n"
    '    """Cast one pair."""\n'
    "    click(card)\n"
    "    # keep the comment\n"
    '    press_key("Space", 0.1)\n'
)
END_TURN = (
    "def end_turn() -> None:\n"
    "    wait_for_ready()\n"
    '    click(selector("Root[0]/Canvas[1]/EndTurn[2]"))\n'
    "\n\n"
    "def wait_for_ready() -> None:\n"
    '    require(scene() == "Battle", "Open Battle first.")\n'
)
SPEAK = (
    "def speak(line: string, times: int = 1) -> None:\n"
    '    press_key("Space", 0.1)\n'
)
CHECKPOINTED = "def look() -> None:\n    checkpoint()\n"


def book_with(**sources: str) -> MacroBook:
    book = MacroBook()
    for name, source in sources.items():
        book.register(macro_definition_from_source(name, source), ())
    return book


def call(
    log: MacroCallLog,
    name: str,
    arguments: dict | None = None,
    scene: str = "Battle",
    after: str = "",
    passed: bool = True,
) -> None:
    log.record_call(
        step=3,
        name=name,
        arguments=arguments or {},
        scene=scene,
        scene_after=after or scene,
        passed=passed,
        paused=False,
    )


def draft(log, book, standing="Reward", taken=None, handled=None):
    return stage_draft_for_visit(
        log, book, standing, taken or set(), set() if handled is None else handled
    )


CARD = {"card": "Root[0]/Canvas[1]/Card[2]"}


def test_a_visit_with_two_macro_calls_becomes_one_draft() -> None:
    log = MacroCallLog()
    call(log, "cast_pair", CARD)
    call(log, "end_turn")
    call(log, "cast_pair", {"card": "Root[0]/Canvas[1]/Other[3]"}, after="Reward")

    found, _why = draft(log, book_with(cast_pair=CAST, end_turn=END_TURN))

    assert found is not None
    assert found.name == "clear_battle"
    assert found.macros == ("cast_pair", "end_turn")
    assert found.calls == 3
    lines = found.source.splitlines()
    assert lines[0] == "def clear_battle() -> None:"
    assert 'require(scene() == "Battle", ' in lines[2]
    assert lines[3] == '    while scene() == "Battle":'
    # 인자는 첫 호출의 것이다.
    assert lines[4] == '        cast_pair(card=selector("Root[0]/Canvas[1]/Card[2]"))'
    assert lines[5] == "        end_turn()"
    # 복사한 원문은 주석과 따옴표를 그대로 지닌다.
    assert "    # keep the comment\n" in found.source
    assert 'press_key("Space", 0.1)' in found.source
    assert "def wait_for_ready() -> None:" in found.source
    macro_definition_from_source(found.name, found.source)


def test_nothing_is_offered_while_the_visit_goes_on_or_the_scene_is_unknown() -> None:
    log = MacroCallLog()
    call(log, "end_turn")
    call(log, "end_turn")
    book = book_with(end_turn=END_TURN)

    assert draft(log, book, standing="Battle")[0] is None
    assert draft(log, book, standing="")[0] is None
    assert draft(log, book, standing="Reward")[0] is not None


def test_one_call_is_not_enough_and_a_visit_is_drafted_once() -> None:
    log = MacroCallLog()
    call(log, "end_turn")
    book = book_with(end_turn=END_TURN)
    handled: set[int] = set()
    assert draft(log, book, handled=handled)[0] is None

    call(log, "end_turn")
    # 첫 시도에서 이 visit 은 이미 따져 봤다.
    assert draft(log, book, handled=handled)[0] is None
    assert draft(log, book, handled=set())[0] is not None


def test_an_earlier_ended_visit_is_still_drafted() -> None:
    """전투 1 에서 두 번 부르고 Shop 에서 한 번 부르면, 가장 최근 호출은 Shop 의 것이다.
    가장 최근 호출만 보면 전투 1 을 영영 놓친다."""
    log = MacroCallLog()
    call(log, "end_turn", scene="Battle")
    call(log, "end_turn", scene="Battle", after="Shop")
    call(log, "speak", {"line": "hi"}, scene="Shop")

    found, _why = draft(log, book_with(end_turn=END_TURN, speak=SPEAK), standing="Map")

    assert found is not None and found.scene == "Battle"


def test_a_visit_back_on_the_same_scene_is_a_new_visit() -> None:
    log = MacroCallLog()
    call(log, "end_turn", scene="Battle")
    call(log, "end_turn", scene="Battle", after="Reward")
    log.record_hand_action(4, "Reward", "Battle")
    call(log, "end_turn", scene="Battle")

    # 두 번째 전투가 아직 진행 중이어도 첫 전투는 끝났다.
    found, _why = draft(log, book_with(end_turn=END_TURN), standing="Battle")

    assert found is not None and found.calls == 2


def test_a_copied_helper_with_an_instance_id_blocks_the_draft() -> None:
    """저장된 macro 는 `#id` 를 담고 있을 수 있다. 그 초안은 `register_macro` 가 거절하므로
    내밀지 않는다."""
    stale = (
        "def tap() -> None:\n"
        '    """Tap one thing."""\n'
        '    click(selector("#-64624"))\n'
    )
    log = MacroCallLog()
    call(log, "tap")
    call(log, "tap", after="Reward")

    found, why = draft(log, book_with(tap=stale))

    assert found is None
    assert "changes between game launches" in why


def test_the_draft_carries_the_screens_the_macros_were_called_on() -> None:
    log = MacroCallLog()
    for after in ("Battle", "Reward"):
        log.record_call(
            step=3, name="end_turn", arguments={}, scene="Battle", scene_after=after,
            passed=True, paused=False, screen="12",
        )

    found, _why = draft(log, book_with(end_turn=END_TURN))

    assert found is not None and found.screens == ("12",)


def test_failed_calls_do_not_count() -> None:
    log = MacroCallLog()
    call(log, "end_turn", passed=False)
    call(log, "end_turn")

    assert draft(log, book_with(end_turn=END_TURN))[0] is None


def test_a_macro_missing_from_the_book_or_with_a_checkpoint_blocks_the_draft() -> None:
    log = MacroCallLog()
    call(log, "end_turn")
    call(log, "look")

    found, why = draft(log, book_with(end_turn=END_TURN))
    assert found is None and "not in this run's book" in why

    found, why = draft(log, book_with(end_turn=END_TURN, look=CHECKPOINTED))
    assert found is None and "checkpoint" in why


def test_object_arguments_must_be_fixed_selector_paths() -> None:
    book = book_with(cast_pair=CAST)
    for bad in ("#123", "640,360", "-", "", "  ", "Root/Card(Clone)[1]"):
        log = MacroCallLog()
        call(log, "cast_pair", {"card": bad})
        call(log, "cast_pair", {"card": bad})
        found, why = draft(log, book)
        assert found is None, bad
        assert "cannot be written as a literal" in why


def test_literal_arguments_are_written_by_declared_type() -> None:
    log = MacroCallLog()
    call(log, "speak", {"line": 'say "hi"\\', "times": 2})
    call(log, "speak", {"line": "x"})

    found, _why = draft(log, book_with(speak=SPEAK))

    assert found is not None
    assert '        speak(line="say \\"hi\\"\\\\", times=2)' in found.source

    log = MacroCallLog()
    call(log, "speak", {"line": 5})
    call(log, "speak", {"line": 5})
    assert draft(log, book_with(speak=SPEAK))[0] is None

    log = MacroCallLog()
    call(log, "speak", {})
    call(log, "speak", {})
    assert draft(log, book_with(speak=SPEAK))[0] is None


def test_the_entry_name_gets_a_suffix_when_it_is_taken() -> None:
    log = MacroCallLog()
    call(log, "end_turn")
    call(log, "end_turn")
    book = book_with(end_turn=END_TURN)

    found, _why = draft(log, book, taken={"clear_battle", "clear_battle_2"})

    assert found is not None and found.name == "clear_battle_3"


def test_colliding_helpers_are_renamed_at_every_call_site() -> None:
    first = "def a() -> None:\n    helper()\n\n\ndef helper() -> None:\n    click(selector(\"X[0]\"))\n"
    second = "def b() -> None:\n    helper()\n\n\ndef helper() -> None:\n    click(selector(\"Y[0]\"))\n"
    log = MacroCallLog()
    call(log, "a")
    call(log, "b")

    found, why = draft(log, book_with(a=first, b=second))

    assert found is not None, why
    assert "def b_helper() -> None:" in found.source
    assert found.source.count("b_helper()") == 2  # def 와 호출 하나
    definition = macro_definition_from_source(found.name, found.source)
    assert {helper.name for helper in definition.helpers} == {"a", "helper", "b", "b_helper"}


def test_an_entry_that_collides_with_an_earlier_helper_is_renamed() -> None:
    first = "def a() -> None:\n    b()\n\n\ndef b() -> None:\n    click(selector(\"X[0]\"))\n"
    second = "def b() -> None:\n    click(selector(\"Y[0]\"))\n"
    log = MacroCallLog()
    call(log, "a")
    call(log, "b")

    found, why = draft(log, book_with(a=first, b=second))

    assert found is not None, why
    assert "        b_2()" in found.source
    macro_definition_from_source(found.name, found.source)


def test_rename_edits_only_names_and_survives_non_ascii_text() -> None:
    source = 'def go() -> None:\n    """한글 helper"""\n    flag("한글 helper"); helper()  # helper\n'
    # `;` 는 문법 밖이라 따로 파싱만 확인하고, 글자 단위 치환만 본다.
    edited = _rename_in_source(
        'def go() -> None:\n    """helper"""\n    helper()\n\n\ndef helper() -> None:\n    flag("helper")\n',
        {"helper": "go_helper"},
    )
    assert edited == (
        'def go() -> None:\n    """helper"""\n    go_helper()\n\n\n'
        'def go_helper() -> None:\n    flag("helper")\n'
    )
    assert "한글" in source


def test_a_draft_that_breaks_the_depth_limit_is_not_offered() -> None:
    deep = (
        "def top() -> None:\n    mid()\n\n\n"
        "def mid() -> None:\n    low()\n\n\n"
        "def low() -> None:\n    click(selector(\"X[0]\"))\n"
    )
    log = MacroCallLog()
    call(log, "top")
    call(log, "top")

    found, why = draft(log, book_with(top=deep))

    assert found is None and "did not parse" in why


def test_scene_names_become_snake_case() -> None:
    assert _snake("StoryScene") == "story_scene"
    assert _snake("Battle 2") == "battle_2"
    assert _snake("HTTPServerScene") == "http_server_scene"
    assert _snake("전투") == "scene"


# --- report_step ---------------------------------------------------------------


def make(stage_draft: bool):
    async def send(frame: dict) -> None:
        pass

    channel = QaRunChannel(qa_try_id=9, send=send, action_timeout=0.05, write_timeout=0.05)
    channel.scene.pulse.apply(
        PulseReading.model_validate({"scene": "Reward", "whole": True, "active": []})
    )
    state = QaRunState(total_steps=3)
    arch = resolve_arch(
        QaArchSpec(
            vision=VisionMode.on,
            phase_cycle=PhaseCycleMode.lite,
            macros="on",
            stage_draft=stage_draft,
        ),
        LLMModel.gpt_6_luna,
    )
    tools = {one.name: one for one in build_tools(channel, state, arch)}
    state.macros.register(macro_definition_from_source("end_turn", END_TURN), ())
    call(state.macro_calls, "end_turn")
    call(state.macro_calls, "end_turn", after="Reward")
    return state, tools


async def report(tools, step: int = 1) -> str:
    return await tools["report_step"].ainvoke(
        {"step": step, "passed": True, "message": "m", "thought": "t", "learned": ""}
    )


def test_report_step_offers_the_stage_draft_through_review_draft() -> None:
    async def scenario() -> None:
        state, tools = make(True)

        answer = await report(tools)

        assert "Stage draft ready" in answer and "`clear_battle`" in answer
        assert state.offered_drafts == {1: "clear_battle"}
        assert "clear_battle" in state.macros.drafts
        assert state.macros.was_read("clear_battle")
        assert state.phase_cycle.phase is RunPhase.review_draft

        # 같은 visit 은 다시 내밀지 않는다.
        again = await report(tools, step=2)
        assert "Stage draft ready" not in again

    asyncio.run(scenario())


def test_registering_a_stage_draft_relates_it_to_the_battle_screen() -> None:
    """등록은 이미 다음 `scene` 에서 일어난다. 서 있는 `screen` 에 맺으면 다음 런은 전투에서
    이 macro 를 못 찾는다."""

    async def scenario() -> None:
        state, tools = make(True)
        state.macro_calls.calls[:] = [
            type(one)(**{**one.__dict__, "screen": "12"}) for one in state.macro_calls.calls
        ]

        await report(tools)
        await tools["register_macro"].ainvoke(
            {"step": 1, "thought": "keep it", "name": "clear_battle"}
        )

        registered = state.macros.registered("clear_battle")
        assert registered is not None
        assert registered.screens == ("12",)

    asyncio.run(scenario())


def test_report_step_offers_nothing_new_when_the_knob_is_off() -> None:
    async def scenario() -> None:
        state, tools = make(False)

        answer = await report(tools)

        assert "Stage draft ready" not in answer
        assert state.offered_drafts == {}

    asyncio.run(scenario())
