"""macro 실행 결과와 현재 scene text 관찰의 경계 검증."""

import asyncio

from tests.test_qa_macro_tools import actions, call, make, with_cards

CHAT = "Root[0]/Canvas[1]/ChatWindow[0]/ChatText[0]"
ADVANCE = (
    "def advance_story() -> None:\n"
    '    """Press Return through the opening."""\n'
    '    press_key("Return", 0.1)\n'
    '    press_key("Return", 0.1)\n'
    '    press_key("Return", 0.1)\n'
)


def test_macro_finishes_and_current_text_remains_observable_without_dialogue_history():
    channel, _state, tools, sent = make()
    with_cards(channel)
    call(tools["write_macro"], name="advance_story", source=ADVANCE)
    call(tools["register_macro"], name="advance_story")
    lines = ["OLD_LINE_A", "OLD_LINE_B", "CURRENT_LINE"]

    async def game():
        replied = 0
        while replied < len(lines):
            if len(actions(sent)) > replied:
                channel.on_pulse({"type": "PULSE", "payload": {"reading": 100 + replied, "frame": 1000 + replied, "scene": "StoryScene", "active": [{"selector": CHAT, "text": lines[replied]}]}})
                channel.on_action_result({"correlationId": actions(sent)[replied]["messageId"], "payload": {"results": [], "frame": 10 + replied}})
                replied += 1
            await asyncio.sleep(0)

    async def drive():
        task = asyncio.create_task(game())
        try:
            return await asyncio.wait_for(tools["run_macro"].ainvoke({"step": 1, "thought": "opening", "name": "advance_story", "arguments": {}}), timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    answer = asyncio.run(drive())
    assert "advance_story ran to the end; 3 action(s) reached the game" in answer
    assert len(actions(sent)) == 3
    assert "OLD_LINE_A" not in answer and "OLD_LINE_B" not in answer
    assert "Text the game showed while the macro ran" not in answer
    current = channel.scene.pulse.render(since=0, advance=False)
    assert "CURRENT_LINE" in current
    assert "OLD_LINE_A" not in current and "OLD_LINE_B" not in current
