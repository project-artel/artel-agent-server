"""Short and folded `run_macro` results (ARTEL-1026, track D).

`fold_stale_macro_results` off leaves the result text as it was. On, a plain success is one
line, every other result keeps its listing inside a marker, and once the step that called it
is reported the listing is replaced by the line the marker carries.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.qa.arch import QaArchSpec, default_resolved_arch, resolve_arch
from app.agents.qa.context import FoldKind, fold_context, fold_every_stale_block
from app.agents.qa.macro.errors import WAIT_TIMEOUT, MacroFailure
from app.agents.qa.macro.runner import MacroFlagged, MacroPlace, MacroRunResult
from app.agents.qa.runner import fold_kinds_for
from app.agents.qa.tools.macro_tools import _render


def finished(actions: int = 15) -> MacroRunResult:
    result = MacroRunResult(name="fifteen")
    result.applied = [MacroPlace("fifteen", index, actions) for index in range(1, actions + 1)]
    result.outcomes = ["  key_click: ok"] * actions
    return result


def stopped() -> MacroRunResult:
    result = finished(3)
    result.failure = MacroFailure(code=WAIT_TIMEOUT, reason="never arrived")
    return result


def test_off_keeps_the_full_listing() -> None:
    text = _render(finished(), "Battle")

    assert "Reached the game:" in text
    assert "<<macro result" not in text


def test_a_plain_success_is_one_line_with_the_scene() -> None:
    text = _render(finished(), "Battle", fold_stale=True)

    assert text == (
        "fifteen ran to the end; 15 action(s) reached the game; on scene Battle at the end."
    )


def test_the_scene_is_left_out_when_unknown() -> None:
    assert "scene" not in _render(finished(), "", fold_stale=True)


def test_a_flagged_result_keeps_its_listing_inside_the_marker() -> None:
    result = finished()
    result.flags = [MacroFlagged(place=MacroPlace("fifteen", 2, 15), message="odd", observed={})]

    text = _render(result, "Battle", fold_stale=True)

    assert text.startswith("<<macro result: fifteen ran to the end; 15 action(s)")
    assert "FLAG at" in text
    assert text.endswith("<<end macro result>>")


def test_a_failed_action_in_a_finished_macro_keeps_its_listing() -> None:
    result = finished(2)
    result.outcomes = ["  key_click: FAILED — no such key", "  key_click: ok"]

    assert "FAILED" in _render(result, "", fold_stale=True)


def test_a_stopped_result_keeps_its_listing_inside_the_marker() -> None:
    text = _render(stopped(), "", fold_stale=True)

    assert "<<macro result: fifteen stopped — WAIT_TIMEOUT; 3 action(s)" in text
    assert "Reached the game: " in text


def call(name: str, call_id: str, step: int) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": {"step": step}, "id": call_id}]
    )


def conversation(result_text: str, reported: bool, reported_before: bool = False) -> list:
    messages: list = [HumanMessage(content="scenario")]
    if reported_before:
        messages += [call("report_step", "r0", 1), ToolMessage(content="ok", tool_call_id="r0")]
    messages += [
        call("run_macro", "m1", 1),
        ToolMessage(content=result_text, tool_call_id="m1", name="run_macro"),
    ]
    if reported:
        messages += [call("report_step", "r1", 1), ToolMessage(content="ok", tool_call_id="r1")]
    return messages


STOPPED_TEXT = _render(stopped(), "Battle", fold_stale=True) + "\n\n<<scene view 4>>\nbody\n<<end scene view>>"


def test_the_listing_is_replaced_by_its_line_once_the_step_is_reported() -> None:
    folded = fold_every_stale_block(conversation(STOPPED_TEXT, True), [FoldKind.macro_result])

    content = folded[2].content
    assert content.startswith("[macro result folded — fifteen stopped — WAIT_TIMEOUT; 3 action(s)")
    assert "Reached the game: " not in content
    # 화면은 marker 밖이라 이 종류의 fold 가 건드리지 않는다.
    assert "<<scene view 4>>" in content


def test_a_result_whose_step_is_not_reported_yet_stays_whole() -> None:
    messages = conversation(STOPPED_TEXT, False)

    folded = fold_every_stale_block(messages, [FoldKind.macro_result])

    assert folded[2] is messages[2]


def test_a_report_before_the_call_does_not_count() -> None:
    messages = conversation(STOPPED_TEXT, False, reported_before=True)

    assert fold_every_stale_block(messages, [FoldKind.macro_result])[4] is messages[4]


def test_resume_macro_results_fold_the_same_way() -> None:
    messages = conversation(STOPPED_TEXT, True)
    messages[1] = call("resume_macro", "m1", 1)

    folded = fold_every_stale_block(messages, [FoldKind.macro_result])

    assert folded[2].content.startswith("[macro result folded")


def test_a_folded_block_stays_folded_on_every_later_call() -> None:
    messages = conversation(STOPPED_TEXT, True)
    first = fold_context(messages, [FoldKind.macro_result], threshold_chars=0)
    later = messages + [call("observe_scene", "o", 2), ToolMessage(content="x", tool_call_id="o")]

    second = fold_context(later, [FoldKind.macro_result], first.folded_keys, threshold_chars=10**9)

    assert second.messages[2].content == first.messages[2].content
    assert not second.newly_folded_kinds


def test_the_fold_waits_for_the_batch_threshold() -> None:
    fold = fold_context(
        conversation(STOPPED_TEXT, True), [FoldKind.macro_result], threshold_chars=10**9
    )

    assert fold.messages[2].content == STOPPED_TEXT


def test_the_knob_turns_the_fold_kind_on() -> None:
    off = resolve_arch(QaArchSpec(fold_stale_macro_results=False), "openai/gpt-6-luna")
    on = default_resolved_arch()

    assert FoldKind.macro_result not in fold_kinds_for(off)
    assert FoldKind.macro_result in fold_kinds_for(on)
