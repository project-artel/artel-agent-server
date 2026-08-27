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


def test_아무것도_모델_뒤에_덧붙지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """매 호출 맨 뒤에 붙었다 사라지는 메시지가 없다.

    그런 꼬리는 프롬프트 접두를 매 턴 깨뜨린다 — 저장된 프롬프트가 새 요청의 완전한
    접두가 되는 일이 영영 없어서, 캐시가 시스템 프롬프트에서 멈춘다. 크기의 문제가
    아니었고 10 토큰짜리 고정 꼬리도 똑같이 깼다(ARTEL-621).

    화면은 도구 결과가 낸다. `create_agent` 가 도구 호출 없는 응답에 루프를 끝내므로
    런 중간의 모든 모델 호출은 직전 도구 결과 뒤에 오고, 꼬리가 메우려던 틈이 없다.
    """
    model, _channel, _sent = scripted_run(monkeypatch)

    first, rest = model.received[0], model.received[1:]
    # 첫 호출은 시스템 프롬프트와 시나리오뿐이다. 아직 도구를 부른 적이 없다.
    assert isinstance(first[-1], HumanMessage)

    # 그 뒤로는 매번 도구 결과가 마지막이다. 미들웨어가 뒤에 얹었다면 그것이 마지막일
    # 것이고, 이 검사가 그것을 잡는다.
    assert rest, "루프가 한 번도 안 돌았다면 이 검사가 아무것도 안 지킨다"
    for received in rest:
        assert isinstance(received[-1], ToolMessage), type(received[-1]).__name__


async def _swallow(frame: dict) -> None:
    return None

def test_컨텍스트_분해가_종류별로_센다():
    """총량만으로는 무엇을 고쳐야 하는지가 안 나온다.

    라이브 뷰를 따로 세던 칸이 있었다. 그 답이 나왔으므로 뺐다 — 매 턴 교체되던 그
    꼬리가 프롬프트 접두를 깨뜨리는 원인이었고, 지금은 화면이 도구 결과 안에 있다."""
    from langchain_core.messages import AIMessage, HumanMessage

    from app.agents.qa.runner import _context_shape

    shape = _context_shape(
        [
            HumanMessage(content="시나리오 " * 200),
            AIMessage(content="추론 " * 100),
        ]
    )

    assert "human=" in shape
    assert "ai=" in shape
    assert "live=" not in shape

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
