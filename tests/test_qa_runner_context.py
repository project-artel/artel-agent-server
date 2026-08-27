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
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.context import DEFAULT_KEEP_SCENES
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState
from app.qa.schemas import QaCaseRef, QaScenario, QaStep
from app.qa.channel import QaRunChannel
from app.qa.envelope import MessageType
from app.qa.scene import CURRENT_SCENE_END, CURRENT_SCENE_START


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
    synchronously, so the tool call resolves without a background task.

    프레임은 게임이 스스로 올린다 — 실제 SDK 의 `PollSceneState` 가 그 모양이고,
    에이전트는 ARTEL-516 이후로 화면을 묻지 않는다. 그래서 붙자마자 한 장 올리고,
    액션이 나갈 때마다 그것이 만든 화면을 다시 올린다.
    """
    sent: list[dict] = []
    tick = 0

    def push() -> None:
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

    async def send(frame: dict) -> None:
        sent.append(frame)
        if frame["type"] != MessageType.ACTION.value:
            return
        push()
        results = [{"id": a["id"], "success": True} for a in frame["payload"]["actions"]]
        channel.on_action_result(
            {"correlationId": frame["messageId"], "payload": {"results": results}}
        )

    channel = QaRunChannel(qa_try_id=1, send=send)
    push()
    return channel, sent


def scenario() -> QaScenario:
    return QaScenario(
        title="시작 확인",
        description="시작 화면이 뜨는지 확인한다.",
        steps=[
            QaStep(action="관찰", case_id=1, case=QaCaseRef(id=1, precondition="메인 메뉴", test_step="시작", expected="화면이 보인다"))
        ],
    )


def tool_result_contents(messages: list[BaseMessage]) -> list[str]:
    return [m.content for m in messages if isinstance(m, ToolMessage) and isinstance(m.content, str)]


def scripted_run(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedModel, QaRunChannel, list[dict]]:
    """Four observations, a verdict and a close, against a model that records
    every message list it was handed."""

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
        lambda _model, reasoning=None, **_: model,
    )

    channel, sent = make_channel()
    state = QaRunState(total_steps=1)
    runner = QaRunner()

    asyncio.run(runner.run(channel, scenario(), state))

    assert state.finished
    assert len(model.received) >= 6
    return model, channel, sent


def test_the_model_receives_folded_views_but_the_channel_keeps_the_full_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four observations happen before the run ends; `DEFAULT_KEEP_SCENES` says
    how many of those tool-result views survive in full — while the channel's own
    scene memory and the sent LOG/STATUS frames are never touched by the fold."""

    model, channel, sent = scripted_run(monkeypatch)

    # The 5th call is the first one made after all four observations landed.
    fifth_call_results = tool_result_contents(model.received[4])
    assert len(fifth_call_results) == 4
    folded = fifth_call_results[: 4 - DEFAULT_KEEP_SCENES]
    kept = fifth_call_results[4 - DEFAULT_KEEP_SCENES :]
    assert all("you can act on:" not in content for content in folded)
    assert all("you can act on:" in content for content in kept)
    assert "observe_scene" in folded[0]

    # Model input only: the channel's own memory and everything actually sent
    # over the socket carry the full views, unaffected by the fold.
    assert "you can act on:" in channel.scene.render(0)
    log_and_status_frames = [
        frame
        for frame in sent
        if frame["type"] in (MessageType.LOG.value, MessageType.STATUS.value)
    ]
    assert any(frame["type"] == MessageType.STATUS.value for frame in log_and_status_frames)


def test_the_current_scene_is_the_last_thing_the_model_reads_every_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of the live view: the agent never has to have asked."""

    model, _channel, _sent = scripted_run(monkeypatch)

    def views(messages: list[BaseMessage]) -> list[BaseMessage]:
        # The system prompt teaches the agent how to read this block, so it names
        # the marker too. Only a message that IS a view counts here.
        return [m for m in messages if str(m.content).startswith(CURRENT_SCENE_START)]

    # 첫 호출에도 실린다. 게임이 붙자마자 프레임을 올리기 때문이다 — 에이전트가 아직
    # 아무것도 묻지 않았는데 화면이 있다는 것이 이 블록의 요점이고, 화면을 묻는 액션이
    # 사라진 뒤(ARTEL-516)로는 **묻는 길이 아예 없다.**
    #
    # 아무것도 도착하지 않은 창은 아래 `..._before_anything_arrives` 가 지킨다.
    for received in model.received:
        last = received[-1]
        assert isinstance(last, HumanMessage)
        assert str(last.content).startswith(CURRENT_SCENE_START)
        assert str(last.content).endswith(CURRENT_SCENE_END)
        # Exactly one, every turn: it is appended to the request rather than
        # written into the graph's state, so it cannot pile up.
        assert views(received) == [last]


def test_no_scene_block_before_anything_arrives() -> None:
    """한 프레임도 못 받았으면 블록이 없다.

    붙자마자 프레임이 온다는 것과 "안 와도 있는 척한다"는 다르다. 뒤의 것이면 에이전트가
    빈 화면을 실제 화면으로 읽는다.
    """
    channel = QaRunChannel(qa_try_id=1, send=_swallow)

    assert channel.scene.render_now() is None


async def _swallow(frame: dict) -> None:
    return None


def test_컨텍스트_분해가_라이브_뷰를_따로_센다():
    """이 줄이 답해야 하는 첫 질문이 그것이다 — 라이브 뷰가 전체의 몇 %인가.

    종류로는 Human 이라 그냥 세면 시나리오와 섞인다. 섞이면 판독 렌더를 줄이는 것이
    의미가 있는지 없는지를 로그가 말해 주지 못한다(ARTEL-604)."""
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agents.qa.runner import _context_shape
    from app.qa.scene import CURRENT_SCENE_START

    shape = _context_shape(
        [
            HumanMessage(content="시나리오 " * 200),
            AIMessage(content="추론 " * 100),
            HumanMessage(content=CURRENT_SCENE_START + "\n판독 " * 300),
        ]
    )

    assert "live=" in shape
    assert "live=0(" not in shape
    # 라이브 뷰가 human 에 섞이지 않았다.
    assert "human=" in shape


def test_컨텍스트_분해가_접힌_뷰와_남은_뷰를_가른다():
    """`fold_stale_scenes` 가 실제로 얼마나 누르는지는 이 둘의 비에서만 나온다."""
    from langchain_core.messages import ToolMessage

    from app.agents.qa.context import _placeholder
    from app.agents.qa.runner import _context_shape
    from app.qa.scene import SCENE_VIEW_END, SCENE_VIEW_START_PREFIX, SCENE_VIEW_START_SUFFIX

    view = f"{SCENE_VIEW_START_PREFIX}7{SCENE_VIEW_START_SUFFIX}\n값들\n{SCENE_VIEW_END}"
    shape = _context_shape(
        [
            ToolMessage(content=_placeholder("3"), tool_call_id="a"),
            ToolMessage(content=_placeholder("4"), tool_call_id="b"),
            ToolMessage(content=view, tool_call_id="c"),
        ]
    )

    assert "views folded=2 kept=1" in shape


def test_컨텍스트_분해가_빈_목록에도_답한다():
    """0 으로 나누지 않는다. 첫 호출 전이나 압축 직후에 실제로 빌 수 있다."""
    from app.agents.qa.runner import _context_shape

    assert _context_shape([]) == "messages=0"


def test_컨텍스트_분해가_런을_죽이지_않는다():
    """여기서 오르는 예외는 모델 호출을 통째로 날린다. 로그 한 줄에는 그것을
    감수할 만한 것이 없다."""
    import asyncio
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage

    from app.agents.qa.runner import _log_token_usage

    async def handler(_request):
        return SimpleNamespace(result=[AIMessage(content="ok")])

    # `messages` 가 없는 request 여도 호출이 지나간다.
    asyncio.run(_log_token_usage.awrap_model_call(object(), handler))
