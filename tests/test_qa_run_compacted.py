"""A run that compacts still reaches `finish_run`.

`tests/test_qa_compaction.py` pins the message surgery. This drives the real
`QaRunner.run` — real `create_agent`, real graph, real tool loop — through an
agent that calls `compact_context` half way, and checks that the run survives it
and that the model is handed something it can carry on from. The wiring is the
thing being tested: compaction that works perfectly in isolation and is attached
to the graph wrongly leaves the run no better off than none at all.
"""

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState
from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.config import get_settings
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, MessageType

SUMMARY_TEXT = "## SCENARIO\nOne scenario.\n\n## NEXT ACTION\nReport step 2."


class ScriptedModel(BaseChatModel):
    """One scripted tool call per turn, recording what it was handed."""

    turns: list[dict[str, Any]]
    received: list[list[BaseMessage]] = []
    calls: int = 0
    bound_tools: list[str] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ScriptedModel":
        self.bound_tools = [getattr(tool, "name", "") for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.received.append(list(messages))
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=turn.get("content", ""),
                        tool_calls=turn.get("tool_calls", []),
                    )
                )
            ]
        )


class Summarizer(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "summarizer"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=SUMMARY_TEXT))])


def make_channel() -> tuple[QaRunChannel, list[dict]]:
    """A channel whose `send` answers ACTION frames the way the game would."""
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
                    "interactables": [{"id": 1, "name": "Start", "type": "button"}],
                    "observables": {"Tick": {"value": tick}},
                },
            }
        )
        channel.on_action_result(
            {
                "correlationId": frame["messageId"],
                "payload": {
                    "results": [{"id": a["id"], "success": True} for a in frame["payload"]["actions"]]
                },
            }
        )

    channel = QaRunChannel(qa_try_id=1, send=send)
    return channel, sent


def scenario() -> ScenarioDraft:
    return ScenarioDraft(
        title="시작 확인",
        description="시작 화면과 시작 버튼을 확인한다.",
        steps=[
            ScenarioStep(step=1, title="시작 화면", state="메인 메뉴", action="관찰", expected="화면이 보인다"),
            ScenarioStep(step=2, title="시작 버튼", state="메인 메뉴", action="관찰", expected="버튼이 보인다"),
        ],
    )


def look(call_id: str) -> dict:
    return {"tool_calls": [{"name": "observe_scene", "args": {"step": 1, "thought": "본다"}, "id": call_id}]}


def verdict(call_id: str, step: int) -> dict:
    return {
        "tool_calls": [
            {
                "name": "report_step",
                "args": {"step": step, "passed": True, "message": f"{step}번 확인함", "thought": "판정"},
                "id": call_id,
            }
        ]
    }


def compacted_run(monkeypatch: pytest.MonkeyPatch) -> tuple[ScriptedModel, QaRunState, list[dict]]:
    """A run that looks three times, reports step 1, compacts, then finishes."""
    model = ScriptedModel(
        turns=[
            look("1"),
            look("2"),
            look("3"),
            verdict("4", 1),
            {"tool_calls": [{"name": "compact_context", "args": {"reason": "길어졌다"}, "id": "5"}]},
            verdict("6", 2),
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "통과", "thought": "종료"},
                        "id": "7",
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    summarizer = Summarizer()

    def build(chosen, reasoning=None, cache_prompt=False, **_):
        # The run's own model asks for prompt caching; the summarizer never does.
        # That is what separates the two call sites here.
        return model if cache_prompt else summarizer

    monkeypatch.setattr("app.agents.qa.runner.build_chat_model", build)

    # Only the agent's own request should compact: the trigger is set to the
    # model's whole budget, which this short run comes nowhere near.
    settings = get_settings().model_copy(
        update={
            "qa_compaction_trigger_fraction": 1.0,
            "qa_compaction_keep_messages": 4,
            "qa_compaction_min_new_messages": 2,
        }
    )

    channel, sent = make_channel()
    state = QaRunState(total_steps=2)
    asyncio.run(QaRunner(settings=settings).run(channel, scenario(), state))
    assert summarizer.calls == 1
    return model, state, sent


def test_a_run_that_compacts_still_reaches_its_verdicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point. A compaction mid-run must cost the run nothing that
    matters: not the verdict it already recorded, not the steps still to do, and
    not its ability to close itself."""
    _model, state, _sent = compacted_run(monkeypatch)

    assert state.compactions == 1
    assert state.finished
    assert [(r.step, r.passed) for r in state.step_results] == [(1, True), (2, True)]


def test_the_turn_after_a_compaction_reads_the_summary_and_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the agent is left holding: a summary of what it did, and the record of
    where the run actually stands."""
    model, _state, _sent = compacted_run(monkeypatch)

    # Turn 6 is the first model call made after `compact_context` was answered.
    after = model.received[5]
    texts = [str(message.content) for message in after]

    assert any(SUMMARY_TEXT in text for text in texts)
    # The ledger's own opening sentence, not the marker alone — the system
    # prompt names the marker too, when it teaches the agent to read the block.
    ledger = next(text for text in texts if text.startswith("CONTEXT COMPACTED."))
    assert "step 1: PASS — 1번 확인함" in ledger
    assert "Continue with step 2" in ledger
    # The conversation really was cut: the opening instructions are gone.
    assert not any("Begin. Observe the screen first." in text for text in texts)


def test_the_operator_is_told_a_compaction_happened(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run whose reasoning suddenly refers to a summary is unreadable without
    knowing one was made."""
    _model, _state, sent = compacted_run(monkeypatch)

    system_notes = [
        frame
        for frame in sent
        if frame["type"] == MessageType.LOG.value
        and frame["payload"]["category"] == LogCategory.SYSTEM.value
    ]
    assert len(system_notes) == 1
    assert "compacted" in system_notes[0]["payload"]["message"]


def test_compaction_off_leaves_both_the_middleware_and_the_tool_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `compact_context` that sets a flag nothing reads is worse than no tool at
    all — the agent would spend calls on it and get nothing."""
    model = ScriptedModel(
        turns=[
            verdict("1", 1),
            verdict("2", 2),
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "통과", "thought": "종료"},
                        "id": "3",
                    }
                ]
            },
            {"content": "done"},
        ]
    )

    def build(chosen, reasoning=None, cache_prompt=False, **_):
        return model

    monkeypatch.setattr("app.agents.qa.runner.build_chat_model", build)

    settings = get_settings().model_copy(update={"qa_compaction_enabled": False})
    channel, _sent = make_channel()
    state = QaRunState(total_steps=2)
    asyncio.run(QaRunner(settings=settings).run(channel, scenario(), state))

    assert state.finished
    assert "compact_context" not in model.bound_tools
