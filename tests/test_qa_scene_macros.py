"""지난 런이 등록한 macro 를 이번 런이 목록으로 만난다 (ARTEL-935).

네 가지를 못박는다. 넷은 서로 다른 방식으로 깨진다:

- **payload 를 읽는가** — orchestration 의 `macros` · `macrosTotal` 이 `SceneContext` 에
  들어오고, 칸이 빠져도 다른 칸을 잃지 않는다.
- **글이 계약대로인가** — 한 줄의 모양, scene 이름 중복 접기, `[no screen yet]`, 자른 수.
- **누구에게 가는가** — `macros=on` 인 런의 첫 메시지에만 붙고, `off` 인 런은 목록이 있어도
  종전과 같은 글을 읽는다. 목록이 비면 아무것도 안 붙는다.
- **접두가 안 깨지는가** — 같은 런의 두 번째 턴에도 첫 메시지가 그대로다(ARTEL-621).
"""

import asyncio
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from tests.test_qa_runner_context import ScriptedModel, make_channel, scenario

from app.agents.qa.arch import PhaseCycleMode, QaArchSpec, resolve_arch
from app.agents.qa.runner import QaRunner, _macro_discovery
from app.agents.qa.tools import QaRunState
from app.llm.models import LLMModel
from app.qa.run_config import resolve_run_config
from app.qa.scene_context import MAX_MACROS_IN_FIRST_MESSAGE, SceneContext


def macro(name: str, **overrides: Any) -> dict:
    payload = {
        "name": name,
        "parameters": [{"name": "times", "type": "int"}],
        "summary": "Step 3: 대화를 끝까지 넘긴다 — 13 actions, drafted from a hand-driven run.",
        "screens": [{"screenId": "69", "sceneName": "StoryScene"}],
    }
    payload.update(overrides)
    return payload


def context(*macros: dict, total: int | None = None) -> SceneContext:
    return SceneContext.model_validate(
        {"macros": list(macros), "macrosTotal": len(macros) if total is None else total}
    )


# --- payload ------------------------------------------------------------------


def test_the_macros_of_the_payload_are_read() -> None:
    parsed = context(macro("replay_step_3"))

    assert parsed.macros_total == 1
    only = parsed.macros[0]
    assert only.name == "replay_step_3"
    assert [(item.name, item.type) for item in only.parameters] == [("times", "int")]
    assert only.summary.startswith("Step 3:")
    assert [(item.screen_id, item.scene_name) for item in only.screens] == [("69", "StoryScene")]


def test_a_payload_without_macros_reads_as_none() -> None:
    """이 칸을 모르는 orchestration. 씬 목록은 종전대로 읽혀야 한다."""
    parsed = SceneContext.model_validate({"gameBuildId": "7", "scenes": []})

    assert parsed.macros == []
    assert parsed.macros_total == 0
    assert parsed.macro_section() == ""


def test_missing_fields_of_one_macro_fall_back_to_defaults() -> None:
    parsed = SceneContext.model_validate(
        {"macros": [{"name": "bare"}, {"name": "nulls", "summary": None, "screens": []}]}
    )

    bare, nulls = parsed.macros
    assert (bare.parameters, bare.summary, bare.screens) == ([], None, [])
    assert nulls.summary is None
    # `macrosTotal` 이 빠져도 목록은 그려진다. 자른 수는 목록 길이로 대신한다.
    assert "bare()" in parsed.macro_section()


def test_an_unknown_field_is_ignored() -> None:
    parsed = SceneContext.model_validate(
        {"macros": [{**macro("a"), "somethingNew": 1}], "alsoNew": True}
    )

    assert parsed.macros[0].name == "a"


# --- 글 -----------------------------------------------------------------------


def test_one_line_per_macro_in_the_agreed_shape() -> None:
    section = context(macro("replay_step_3")).macro_section()

    assert (
        "  replay_step_3(times: int) — Step 3: 대화를 끝까지 넘긴다 — 13 actions, "
        "drafted from a hand-driven run.  [StoryScene]"
    ) in section.splitlines()


def test_the_section_says_where_the_macros_came_from_and_what_to_do_once() -> None:
    section = context(macro("a"), macro("b")).macro_section()

    instruction = [line for line in section.splitlines() if line.startswith("These were registered")]
    assert len(instruction) == 1
    assert "run it with `run_macro` as that step's first action" in instruction[0]
    assert "`read_macro` shows its source" in instruction[0]
    assert "Check the scene first" in instruction[0]


def test_a_macro_without_a_screen_says_so() -> None:
    section = context(macro("floating", screens=[], parameters=[], summary=None)).macro_section()

    assert "  floating()  [no screen yet]" in section.splitlines()


def test_scene_names_are_listed_once_in_the_order_they_came() -> None:
    screens = [
        {"screenId": "1", "sceneName": "StoryScene"},
        {"screenId": "2", "sceneName": "Battle"},
        {"screenId": "3", "sceneName": "StoryScene"},
    ]

    section = context(macro("shared", screens=screens)).macro_section()

    assert section.count("StoryScene") == 1
    assert "[StoryScene, Battle]" in section


def test_a_long_summary_is_clipped_to_one_line() -> None:
    section = context(macro("wordy", summary="길다 " * 200)).macro_section()

    line = next(line for line in section.splitlines() if line.startswith("  wordy("))
    assert len(line) < 260
    assert line.count("\n") == 0 and "…" in line


def test_the_list_is_bounded_and_the_cut_is_said() -> None:
    names = [f"m{index:02d}" for index in range(30)]

    section = context(*(macro(name) for name in names), total=45).macro_section()

    assert section.count("(times: int)") == MAX_MACROS_IN_FIRST_MESSAGE
    assert "m11(" in section and "m12(" not in section
    # 서버가 30개로 자른 것까지 센다: 12개만 보이고 45개 중 33개가 잘렸다.
    assert "showing 12 of 45 macros; 33 cut for space" in section


def test_a_list_that_fits_has_no_cut_note() -> None:
    section = context(macro("a"), macro("b")).macro_section()

    assert "cut for space" not in section


def test_an_empty_list_renders_nothing() -> None:
    assert context().macro_section() == ""
    assert context(total=3).macro_section() == ""


# --- 누구의 첫 메시지에 붙는가 ---------------------------------------------------


def arch_of(macros: str):
    return resolve_arch(
        QaArchSpec(phase_cycle=PhaseCycleMode.off, macros=macros), LLMModel.gpt_6_luna
    )


def channel_with(scene_context: SceneContext | None):
    channel, _sent = make_channel()
    channel.scene.scene_context = scene_context
    return channel


def test_a_run_with_macros_on_gets_the_section() -> None:
    channel = channel_with(context(macro("replay_step_3")))

    assert "replay_step_3(times: int)" in _macro_discovery(arch_of("on"), channel)


def test_a_run_with_macros_off_gets_nothing_even_when_the_build_has_macros() -> None:
    channel = channel_with(context(macro("replay_step_3")))

    assert _macro_discovery(arch_of("off"), channel) == ""


@pytest.mark.parametrize("scene_context", [None, SceneContext(), context()])
def test_a_run_without_a_list_gets_nothing(scene_context: SceneContext | None) -> None:
    assert _macro_discovery(arch_of("on"), channel_with(scene_context)) == ""


def run_and_collect(monkeypatch: pytest.MonkeyPatch, macros: str, scene_context):
    """관측 두 번과 종료. 모델이 매 호출에서 받은 메시지를 돌려준다."""
    model = ScriptedModel(
        turns=[
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "본다"}, "id": "1"}]},
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "또 본다"}, "id": "2"}]},
            {"tool_calls": [{"name": "report_step", "args": {"step": 1, "passed": True, "message": "ok", "thought": "판정"}, "id": "3"}]},
            {"tool_calls": [{"name": "finish_run", "args": {"passed": True, "summary": "통과", "thought": "끝"}, "id": "4"}]},
            {"content": "done"},
        ]
    )
    monkeypatch.setattr(
        "app.agents.qa.runner.build_chat_model", lambda _model, reasoning=None, **_: model
    )
    channel = channel_with(scene_context)
    runner = QaRunner(
        resolve_run_config(arch=QaArchSpec(phase_cycle=PhaseCycleMode.off, macros=macros))
    )
    asyncio.run(runner.run(channel, scenario(), QaRunState(total_steps=1)))
    return model.received


def opening_message(received_turn) -> str:
    humans = [message for message in received_turn if isinstance(message, HumanMessage)]
    return humans[0].content


def test_the_opening_message_of_a_real_run_carries_the_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = run_and_collect(monkeypatch, "on", context(macro("replay_step_3")))

    opening = opening_message(received[0])
    assert "Begin. Observe the screen first." in opening
    assert opening.index("Begin. Observe") < opening.index("Macros registered by earlier runs")
    assert "replay_step_3(times: int)" in opening


def test_the_opening_message_does_not_change_between_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프롬프트 접두가 안 깨진다 (ARTEL-621). 시스템 프롬프트와 첫 메시지가 모든 턴에서 같다."""
    received = run_and_collect(monkeypatch, "on", context(macro("replay_step_3"), macro("b")))

    assert len(received) >= 4
    first_two = [(type(message), message.content) for message in received[0][:2]]
    for turn in received[1:]:
        assert [(type(message), message.content) for message in turn[:2]] == first_two
    # 목록은 첫 메시지에만 한 번 있다. 도구 결과 어디에도 다시 실리지 않는다.
    for turn in received:
        assert sum("replay_step_3(times: int)" in str(message.content) for message in turn) == 1


def test_a_run_with_macros_off_reads_what_it_read_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with_list = run_and_collect(monkeypatch, "off", context(macro("replay_step_3")))
    without_list = run_and_collect(monkeypatch, "off", None)

    assert opening_message(with_list[0]) == opening_message(without_list[0])
    assert "Macros registered" not in opening_message(with_list[0])
