"""PE 엔진의 화면 그림: planner 와 evaluator 가 화면 한 장을 함께 받는다. Jev 는 받지 않는다."""

import asyncio

from langchain_core.messages import HumanMessage

from app.agents.evaluator import verdict as verdict_module
from app.agents.evaluator.verdict import Evaluator, Verdict
from app.agents.planner_executer import planner as planner_module
from app.agents.planner_executer import runner as runner_module
from app.agents.planner_executer.screen_capture import ScreenImage
from app.llm.chat_model import prompt_messages
from tests.test_planner_executer_steps import (
    MACROS,
    PLAYED,
    Asked,
    FakeJev,
    _config,
    _judge,
    _menu,
    _run,
    _scenario,
    _verdicts,
)

IMAGE = ScreenImage("cap-7", "image/jpeg", "AAAA")


def _captures(monkeypatch) -> list[str]:
    why: list[str] = []

    async def capture(channel, reason, step=None):
        why.append(reason)
        return IMAGE

    monkeypatch.setattr(planner_module, "capture_screen", capture)
    monkeypatch.setattr(runner_module, "capture_screen", capture)
    return why


def test_the_planner_and_the_evaluator_each_take_one_picture_and_the_ids_reach_the_logs(monkeypatch) -> None:
    why = _captures(monkeypatch)
    fake = _menu()
    judge, seen = _judge({"passed": True, "reason": "The map opens.", "state_detail": "", "insight": ""})

    state, failure = _run(fake, Asked(MACROS), judge, FakeJev(), screen_images="on")

    assert failure is None and state.finished
    assert why == ["Step 1: capturing the screen for the planner", "Step 1: capturing the screen for the evaluator"]
    decision = next(f["payload"]["decision"] for f in fake.frames("LOG") if f["payload"].get("kind") == "step_macros")
    assert decision["screen_capture_id"] == "cap-7"
    (verdict,) = _verdicts(fake)
    assert verdict["verdict"]["screen_capture_id"] == "cap-7"
    assert "A picture of the screen when play stopped is attached." in seen[0][1]


def test_with_screen_images_off_nothing_is_captured(monkeypatch) -> None:
    why = _captures(monkeypatch)
    fake = _menu()
    judge, seen = _judge({"passed": True, "reason": "The map opens.", "state_detail": "", "insight": ""})

    _run(fake, Asked(MACROS), judge, FakeJev(), screen_images="off")

    assert why == []
    assert "picture" not in seen[0][1]
    assert "screen_capture_id" not in _verdicts(fake)[0]["verdict"]


def test_the_evaluator_sends_the_picture_after_the_request_text(monkeypatch) -> None:
    sent: list = []

    class Chain:
        async def ainvoke(self, messages):
            sent.extend(messages)
            return Verdict(passed=True, reason="ok", state_detail="", insight="")

    monkeypatch.setattr(verdict_module, "structured", lambda *args: Chain())

    verdict = asyncio.run(Evaluator(_config()).judge(_scenario(), 1, PLAYED, None, IMAGE))

    user = sent[1]
    assert isinstance(user, HumanMessage) and user.content[1] == IMAGE.block()
    assert user.content[0]["text"].endswith("A picture of the screen when play stopped is attached.\n")
    assert verdict.screen_capture_id == "cap-7"


def test_the_picture_rides_after_the_cached_prefix() -> None:
    prefix = "map " * 2000
    messages = prompt_messages("claude/claude-sonnet-5-5", "system " * 2000, prefix + "step", prefix, [IMAGE.block()])

    blocks = messages[1].content
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1] == {"type": "text", "text": "step"} and blocks[2] == IMAGE.block()
    plain = prompt_messages("openrouter/gpt", "s", "u", images=[IMAGE.block()])
    assert plain[1].content == [{"type": "text", "text": "u"}, IMAGE.block()]
