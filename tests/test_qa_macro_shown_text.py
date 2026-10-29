"""macro 가 도는 동안 게임이 띄운 글자를 macro 답에 싣는 규칙 (ARTEL-1222).

2026-10-22 측정에서 오프닝 한 줄을 `[MISSING TEXT: STORY_003]` 로 바꾼 빌드를 3런 중 0런이
찾았다. `advance_story` 가 Return 을 12~22회 누르는 동안 지나간 대사는 아무도 읽지 않았다.
"""

import asyncio

from app.agents.qa.tools.macro_tools import MAX_SHOWN_LINES, render_shown_text
from app.qa.pulse import PulseReading
from app.qa.shown_text import ShownTextLog
from tests.test_qa_macro_tools import actions, call, make, with_cards

CHAT = "Root[0]/Canvas[1]/ChatWindow[0]/ChatText[0]"


def said(text: str, reading: int, selector: str = CHAT, scene: str = "StoryScene") -> PulseReading:
    return PulseReading.model_validate(
        {
            "reading": reading,
            "frame": reading * 10,
            "scene": scene,
            "active": [{"selector": selector, "text": text}],
        }
    )


def test_each_new_text_is_a_line_and_a_typewriter_grows_the_same_line() -> None:
    log = ShownTextLog()
    start = log.mark()
    for reading, text in enumerate(["Hel", "Hello", "Hello, Hero.", "[MISSING TEXT: STORY_003]", ""], 1):
        log.apply(said(text, reading))

    lines = log.since(start)
    assert [line.text for line in lines] == ["Hello, Hero.", "[MISSING TEXT: STORY_003]", ""]
    assert {line.source for line in lines} == {"ChatText[0]"}


def test_since_returns_only_what_came_after_the_mark() -> None:
    log = ShownTextLog()
    log.apply(said("before", 1))
    mark = log.mark()
    log.apply(said("after", 2))
    assert [line.text for line in log.since(mark)] == ["after"]


def test_two_objects_keep_two_sets_of_lines() -> None:
    log = ShownTextLog()
    log.apply(said("Grandpa", 1, selector="Root[0]/Canvas[1]/ChatWindow[0]/Name[1]"))
    log.apply(said("Long ago...", 2))
    assert [(line.source, line.text) for line in log.since(0)] == [
        ("Name[1]", "Grandpa"),
        ("ChatText[0]", "Long ago..."),
    ]


def test_the_rendered_block_names_blank_lines_and_asks_for_report_issue() -> None:
    log = ShownTextLog()
    log.apply(said("Long ago...", 1))
    log.apply(said("", 2))
    rendered = render_shown_text(log.since(0))

    assert rendered.startswith("Text the game showed while the macro ran (2 line(s), oldest first):")
    assert '  ChatText[0]: "Long ago..."' in rendered
    assert "  ChatText[0]: (blank)" in rendered
    assert "`report_issue`" in rendered
    assert render_shown_text([]) == ""


def test_the_rendered_block_is_capped() -> None:
    log = ShownTextLog()
    for reading in range(1, MAX_SHOWN_LINES + 6):
        log.apply(said(f"line {reading}", reading))
    rendered = render_shown_text(log.since(0))
    assert "(5 later line(s) not listed)" in rendered
    assert f'"line {MAX_SHOWN_LINES + 1}"' not in rendered


ADVANCE = (
    "def advance_story() -> None:\n"
    '    """Press Return through the opening."""\n'
    '    press_key("Return", 0.1)\n'
    '    press_key("Return", 0.1)\n'
    '    press_key("Return", 0.1)\n'
)


def test_run_macro_lists_every_line_the_presses_went_past() -> None:
    channel, _state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name="advance_story", source=ADVANCE)
    call(tools["register_macro"], name="advance_story")
    opening = ["Long ago...", "[MISSING TEXT: STORY_003]", "The end."]

    async def game() -> None:
        replied = 0
        for _ in range(4000):
            if len(actions(sent)) > replied:
                # 키 하나에 대사 한 줄이 바뀌고, 그 다음에 결과가 온다.
                channel.on_pulse({"type": "PULSE", "payload": said(opening[replied], 100 + replied).model_dump(by_alias=True, exclude_none=True)})
                channel.on_action_result(
                    {
                        "correlationId": actions(sent)[replied]["messageId"],
                        "payload": {"results": [], "frame": 10 + replied},
                    }
                )
                replied += 1
                if replied >= len(opening):
                    return
            await asyncio.sleep(0)

    async def drive() -> str:
        replier = asyncio.create_task(game())
        answer = await tools["run_macro"].ainvoke(
            {"step": 1, "thought": "opening", "name": "advance_story", "arguments": {}}
        )
        replier.cancel()
        return answer

    answer = asyncio.run(drive())

    assert "advance_story ran to the end; 3 action(s) reached the game" in answer
    assert "Text the game showed while the macro ran (3 line(s), oldest first):" in answer
    for line in opening:
        assert f'ChatText[0]: "{line}"' in answer
    # 글자 줄이 붙은 결과는 끝까지 갔어도 marker 로 감싸 `fold` 가 줄째로 접는다.
    assert answer.startswith("<<macro result: advance_story ran to the end")
    assert "<<end macro result>>" in answer
