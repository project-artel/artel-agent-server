"""`QaRunner` actually folds stale scene views out of what the model sees.

`tests/test_qa_agents_context.py` pins `fold_stale_scenes` in isolation. This
drives a real `QaRunner.run` — real `create_agent`, real tool loop, real
`QaRunChannel` — with a fake model standing in for the LLM, and inspects
exactly what that model received on each turn. The point is the wiring, not
the fold itself: a correct pure function wired to nothing would leave the
run exactly as prone to the runaway context growth this whole feature exists
to fix.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState
from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType


class ScriptedModel(BaseChatModel):
    """Returns one scripted tool call per turn, and records what it was given."""

    turns: list[dict[str, Any]]
    received: list[list[BaseMessage]] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.received.append(list(messages))
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        message = AIMessage(content=turn.get("content", ""), tool_calls=turn.get("tool_calls", []))
        return ChatResult(generations=[ChatGeneration(message=message)])


def make_channel() -> tuple[QaRunChannel, list[dict]]:
    """A channel whose `send` answers ACTION frames the way the game would,
    synchronously, so the tool call resolves without a background task."""
    sent: list[dict] = []
    tick = 0

    async def send(frame: dict) -> None:
        sent.append(frame)
        if frame["type"] != MessageType.ACTION.value:
            return
        nonlocal tick
        tick += 1
        channel.on_game_state(
            {
                "type": "GAME_STATE",
                "payload": {
                    "scene": "MainMenu",
                    "interactables": [
                        {"id": i, "name": f"Button{i}", "type": "button"} for i in range(1, 6)
                    ],
                    "observables": {"Tick": {"value": tick}},
                },
            }
        )
        results = [{"id": a["id"], "success": True} for a in frame["payload"]["actions"]]
        channel.on_action_result(
            {"correlationId": frame["messageId"], "payload": {"results": results}}
        )

    channel = QaRunChannel(qa_try_id=1, send=send)
    return channel, sent


def scenario() -> ScenarioDraft:
    return ScenarioDraft(
        title="시작 확인",
        description="시작 화면이 뜨는지 확인한다.",
        steps=[
            ScenarioStep(step=1, title="시작", state="메인 메뉴", action="관찰", expected="화면이 보인다")
        ],
    )


def tool_result_contents(messages: list[BaseMessage]) -> list[str]:
    return [m.content for m in messages if isinstance(m, ToolMessage) and isinstance(m.content, str)]


def test_the_model_receives_folded_views_but_the_channel_keeps_the_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four observations happen before the run ends; DEFAULT_KEEP_SCENES=2 means
    the model's LAST call should see the first two folded and the last two in
    full — while the channel's own scene memory and the sent LOG/STATUS frames
    are never touched by the fold at all."""

    model = ScriptedModel(
        turns=[
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "본다"}, "id": "1"}]},
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "다시 본다"}, "id": "2"}]},
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "또 본다"}, "id": "3"}]},
            {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "마지막으로 본다"}, "id": "4"}]},
            {
                "tool_calls": [
                    {
                        "name": "report_step",
                        "args": {"step": 1, "passed": True, "message": "화면 확인함", "thought": "판정"},
                        "id": "5",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "통과", "thought": "종료"},
                        "id": "6",
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    monkeypatch.setattr(
        "app.agents.qa.runner.build_chat_model",
        lambda _model, reasoning=None: model,
    )

    channel, sent = make_channel()
    state = QaRunState(total_steps=1)
    runner = QaRunner()

    asyncio.run(runner.run(channel, scenario(), state))

    assert state.finished
    assert len(model.received) >= 6

    # The 5th call is the first one made after all four observations landed.
    fifth_call_results = tool_result_contents(model.received[4])
    assert len(fifth_call_results) == 4
    assert "you can act on:" not in fifth_call_results[0]
    assert "you can act on:" not in fifth_call_results[1]
    assert "you can act on:" in fifth_call_results[2]
    assert "you can act on:" in fifth_call_results[3]
    assert "observe_scene" in fifth_call_results[0]

    # Model input only: the channel's own memory and everything actually sent
    # over the socket carry the full views, unaffected by the fold.
    assert "you can act on:" in channel.scene.render(0)
    log_and_status_frames = [
        frame
        for frame in sent
        if frame["type"] in (MessageType.LOG.value, MessageType.STATUS.value)
    ]
    assert any(frame["type"] == MessageType.STATUS.value for frame in log_and_status_frames)
