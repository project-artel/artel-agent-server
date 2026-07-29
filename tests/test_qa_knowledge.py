"""The knowledge search tool: the round trip, and the four ways it does not work.

The tool is the only one that talks past the game — its answer comes from
Orchestration's knowledge base rather than the SDK — so nothing in
`tests/test_qa_tools.py` exercises this path. What is pinned here is what the
run keeps doing when the answer is empty, late, refused, or spent: in every one
of those the agent has to come back with something it can judge a step on,
because a knowledge lookup is a side errand to the verdict and never a reason to
stop.
"""

import asyncio

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.qa.knowledge import (
    KNOWLEDGE_TAGS,
    MAX_DESCRIPTION_CHARS,
    MAX_SEARCHES_PER_RUN,
    RESULT_LIMIT,
    render_description,
)
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory, MessageType
from app.qa.scene import SCENE_VIEW_START_PREFIX


def make(timeout: float = 0.05):
    """A channel whose knowledge search times out fast unless a test answers it."""
    sent: list[dict] = []

    async def send(frame: dict) -> None:
        sent.append(frame)

    channel = QaRunChannel(qa_try_id=7, send=send, action_timeout=timeout)
    state = QaRunState(total_steps=1)
    tools = {tool.name: tool for tool in build_tools(channel, state)}
    return channel, state, tools, sent


def searches(sent: list[dict]) -> list[dict]:
    return [f for f in sent if f["type"] == MessageType.KNOWLEDGE_SEARCH.value]


def hit(**overrides) -> dict:
    return {
        "id": "41",
        "tag": "RULE",
        "source": "DOCS",
        "summary": "구매는 소지금이 가격 이상일 때만 가능하다",
        "description": "소지금이 가격보다 적으면 구매 버튼은 비활성 상태가 되고 눌러도 반응하지 않는다.",
        "score": 0.83,
        **overrides,
    }


def answer(
    channel: QaRunChannel,
    sent: list[dict],
    payload: dict,
    type_: str | None = None,
    correlation: str | None = None,
):
    """Reply the way Orchestration does, quoting the request's messageId.

    Waits for the frame to actually go out rather than assuming it is already
    there when the replying task first runs. `correlation` overrides that echo,
    which is how a reply to somebody else's request is simulated.
    """
    already = len(searches(sent))

    async def reply() -> None:
        for _ in range(50):
            if len(searches(sent)) > already:
                break
            await asyncio.sleep(0)
        frame = {
            "type": type_ or MessageType.KNOWLEDGE_SEARCH_RESULT.value,
            "correlationId": correlation or searches(sent)[-1]["messageId"],
            "payload": payload,
        }
        if frame["type"] == MessageType.ERROR.value:
            channel.on_error(frame)
        else:
            channel.on_knowledge_search_result(frame)

    return asyncio.create_task(reply())


# --- the round trip -----------------------------------------------------------


def test_a_search_goes_out_as_a_knowledge_search_frame() -> None:
    """The type and the payload shape are the contract with Orchestration."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "골드 부족", "model": "e5", "results": [hit()]})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 2, "thought": "구매 실패 규칙을 확인한다", "query": "골드가 모자라면 어떻게 되나"}
        )

        frame = searches(sent)[-1]
        assert frame["payload"] == {
            "query": "골드가 모자라면 어떻게 되나",
            "tag": None,
            "limit": RESULT_LIMIT,
        }
        assert "구매는 소지금이" in result

    asyncio.run(run())


def test_the_reason_for_searching_reaches_the_timeline() -> None:
    """`thought` is the only record of why the run spent a search."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": []})
        await tools["search_knowledge"].ainvoke(
            {"step": 3, "thought": "규칙이 애매해 문서를 본다", "query": "구매 규칙"}
        )

        rows = [f for f in sent if f["type"] == MessageType.LOG.value]
        assert [(r["payload"]["message"], r["payload"]["step"]) for r in rows] == [
            ("규칙이 애매해 문서를 본다", 3)
        ]
        assert rows[0]["payload"]["category"] == LogCategory.THOUGHT.value

    asyncio.run(run())


def test_a_tag_narrows_the_search_and_travels_as_the_singular_field() -> None:
    """Orchestration takes `tag` or `tags`; this side settled on the singular."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": [hit()]})
        await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "규칙만 본다", "query": "구매 규칙", "tag": "rule"}
        )

        assert searches(sent)[-1]["payload"]["tag"] == "RULE"

    asyncio.run(run())


def test_a_hit_carries_where_it_came_from_and_how_close_it_is() -> None:
    """A weak match is still returned, so the agent must be able to discount it."""

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(
            channel,
            sent,
            {"query": "q", "model": "e5", "results": [hit(score=0.41, tag="OBJECTIVE")]},
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "승리 조건"}
        )

        assert "OBJECTIVE" in result
        assert "DOCS" in result
        assert "0.41" in result

    asyncio.run(run())


def test_a_long_description_is_clipped_and_says_so() -> None:
    """Results are never folded, so one verbose entry would sit in context for
    the rest of the run. Clipped rather than dropped: the agent still gets the
    beginning, and the marker tells it there was more."""
    clipped = render_description("가" * (MAX_DESCRIPTION_CHARS + 200))

    assert "[truncated]" in clipped
    assert len(clipped) < MAX_DESCRIPTION_CHARS + 200
    assert render_description("짧다") == "짧다"


# --- empty, late, refused, spent ---------------------------------------------


def test_an_empty_result_is_answered_as_an_answer() -> None:
    """The backfill is asynchronous, so "nothing indexed yet" is a normal state.

    Reported as an error it would push the agent to retry or to fail the step
    over knowledge that was never going to arrive during this run.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(channel, sent, {"query": "q", "model": "e5", "results": []})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "not an error" in result
        assert "Judge this step on what you can see" in result

    asyncio.run(run())


def test_a_search_nobody_answers_gives_up_and_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run must not sit on a knowledge lookup; nothing answers here at all.

    The timeout is shortened rather than waited out — what is under test is what
    the agent is told afterwards, not how long the wait is.
    """
    monkeypatch.setattr("app.qa.channel.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 0.01)

    async def run() -> None:
        _, _, tools, _ = make()

        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "did not answer in time" in result

    asyncio.run(run())


def test_a_refused_search_keeps_the_run_going() -> None:
    """Orchestration answers a failed search with an ERROR on the same correlation.

    Its payload carries only `message` — no `code` — so the frame must be read
    leniently. Validated as a full ErrorPayload it would be dropped, and the tool
    that asked would hang to its timeout instead of being released.
    """

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(
            channel,
            sent,
            {"message": "KNOWLEDGE_SEARCH failed: embedding model mismatch"},
            type_=MessageType.ERROR.value,
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "embedding model mismatch" in result
        assert "says nothing about the game" in result

    asyncio.run(run())


def test_the_per_run_cap_is_refused_with_its_reason() -> None:
    """Same reason as the capture budget: a run that keeps looking things up
    instead of deciding reaches its deadline with no verdict to report."""

    async def run() -> None:
        channel, state, tools, sent = make()
        state.knowledge_searches_attempted = MAX_SEARCHES_PER_RUN

        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert str(MAX_SEARCHES_PER_RUN) in result
        assert searches(sent) == []

    asyncio.run(run())


def test_a_failed_search_still_spends_the_run_budget() -> None:
    """Counting only what worked would leave a failing search unbounded."""

    async def run() -> None:
        channel, state, tools, sent = make()

        answer(channel, sent, {"message": "no"}, type_=MessageType.ERROR.value)
        await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert state.knowledge_searches_attempted == 1

    asyncio.run(run())


def test_an_unknown_tag_costs_neither_a_round_trip_nor_a_search() -> None:
    """Orchestration rejects the whole search on a tag it does not know, so the
    check belongs on this side of the socket."""

    async def run() -> None:
        _, state, tools, sent = make()

        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙", "tag": "GAMEPLAY"}
        )

        assert searches(sent) == []
        assert state.knowledge_searches_attempted == 0
        for name in KNOWLEDGE_TAGS:
            assert name in result

    asyncio.run(run())


def test_a_reply_to_a_foreign_request_does_not_resolve_this_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reply that arrives after its own search gave up must not be handed to
    whatever asked next — the agent would read one question's answer as another's."""
    monkeypatch.setattr("app.qa.channel.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 0.05)

    async def run() -> None:
        channel, _, tools, sent = make()

        answer(
            channel,
            sent,
            {"query": "q", "model": "e5", "results": [hit()]},
            correlation="a-request-we-already-gave-up-on",
        )
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "did not answer in time" in result
        assert "구매는 소지금이" not in result

    asyncio.run(run())


# --- ARTEL-180: a lookup must not drag a scene view back in -------------------


def test_a_search_neither_touches_the_game_nor_returns_a_scene() -> None:
    """The regression this tool was most likely to cause.

    Every acting tool answers with the scene it produced, and `fold_stale_scenes`
    exists because those views pile up. A search changes no screen, so returning
    one would re-spend the context that fold was written to save — and it would
    also cost a game round trip for a picture the agent already has.
    """

    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_game_state(
            {
                "payload": {
                    "scene": "Shop",
                    "interactables": [{"id": 1, "name": "Buy", "type": "button"}],
                }
            }
        )
        frames_before = channel.scene.frames

        answer(channel, sent, {"query": "q", "model": "e5", "results": [hit()]})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert SCENE_VIEW_START_PREFIX not in result
        assert "you can act on:" not in result
        # No ACTION went out, so nothing asked the game for a scene either.
        assert [f for f in sent if f["type"] == MessageType.ACTION.value] == []
        assert channel.scene.frames == frames_before

    asyncio.run(run())


# --- what the model is handed -------------------------------------------------


def test_the_description_states_its_budget_and_its_tags() -> None:
    """An agent that learns the limit by hitting it has already spent it, and a
    tag list that drifts from Orchestration's turns every filter into a refusal."""
    _, _, tools, _ = make()
    description = tools["search_knowledge"].description

    assert str(MAX_SEARCHES_PER_RUN) in description
    for name in KNOWLEDGE_TAGS:
        assert name in description


def test_the_description_says_when_not_to_use_it() -> None:
    """The load-bearing half. Without it the agent searches every step, which
    costs the tool-call budget and returns documentation where it should have
    looked at the screen."""
    _, _, tools, _ = make()
    description = tools["search_knowledge"].description

    assert "Do NOT" in description
    assert "observe_scene" in description


def test_an_operator_message_still_reaches_the_agent_through_a_search() -> None:
    """Operator words ride on the next tool result whatever that tool is; a
    search that swallowed them would lose an instruction the run must obey."""

    async def run() -> None:
        channel, _, tools, sent = make()
        channel.on_chat({"payload": {"message": "상점은 건너뛰세요"}})

        answer(channel, sent, {"query": "q", "model": "e5", "results": []})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "상점은 건너뛰세요" in result

    asyncio.run(run())


# --- the whole loop -----------------------------------------------------------


class ScriptedModel(BaseChatModel):
    """Returns one scripted tool call per turn, and records what it was given."""

    turns: list[dict]
    received: list[list[BaseMessage]] = []
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs) -> "ScriptedModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
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


def test_a_run_searches_knowledge_and_carries_on_to_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring, end to end: a real `QaRunner`, a real tool loop, a real channel.

    A tool that works when invoked directly and stalls inside the graph would
    look identical in every test above. What this pins is that the answer comes
    back as a tool result the next model turn can actually read, and that the run
    goes on to report and finish.
    """
    model = ScriptedModel(
        turns=[
            {
                "tool_calls": [
                    {
                        "name": "search_knowledge",
                        "args": {
                            "step": 1,
                            "thought": "구매 실패 규칙을 확인한다",
                            "query": "골드가 모자라면 구매 버튼이 어떻게 되나",
                            "tag": "RULE",
                        },
                        "id": "1",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "observe_scene",
                        "args": {"step": 1, "thought": "화면을 본다"},
                        "id": "2",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "report_step",
                        "args": {
                            "step": 1,
                            "passed": True,
                            "message": "문서대로 버튼이 비활성이다",
                            "thought": "판정",
                        },
                        "id": "3",
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "name": "finish_run",
                        "args": {"passed": True, "summary": "통과", "thought": "종료"},
                        "id": "4",
                    }
                ]
            },
            {"content": "done"},
        ]
    )
    monkeypatch.setattr("app.agents.qa.runner.build_chat_model", lambda _model: model)

    sent: list[dict] = []

    async def send(frame: dict) -> None:
        """Answer both correspondents the way they answer in production.

        Orchestration replies to a KNOWLEDGE_SEARCH; the game replies to an
        ACTION. Both resolve synchronously here so the loop needs no background
        task to make progress.
        """
        sent.append(frame)
        if frame["type"] == MessageType.KNOWLEDGE_SEARCH.value:
            channel.on_knowledge_search_result(
                {
                    "correlationId": frame["messageId"],
                    "payload": {"query": "골드", "model": "e5", "results": [hit()]},
                }
            )
            return
        if frame["type"] != MessageType.ACTION.value:
            return
        channel.on_game_state(
            {"payload": {"scene": "Shop", "interactables": [], "observables": {}}}
        )
        channel.on_action_result(
            {
                "correlationId": frame["messageId"],
                "payload": {
                    "results": [
                        {"id": a["id"], "success": True} for a in frame["payload"]["actions"]
                    ]
                },
            }
        )

    channel = QaRunChannel(qa_try_id=1, send=send)
    state = QaRunState(total_steps=1)
    scenario = ScenarioDraft(
        title="구매 실패 확인",
        description="골드가 모자랄 때 구매가 막히는지 확인한다.",
        steps=[
            ScenarioStep(
                step=1,
                title="구매 시도",
                state="상점",
                action="구매를 누른다",
                expected="구매가 되지 않는다",
            )
        ],
    )

    asyncio.run(QaRunner().run(channel, scenario, state))

    assert state.finished
    assert len(searches(sent)) == 1
    assert state.knowledge_searches_attempted == 1

    # The second model turn is the first one made after the search answered.
    knowledge_result = [
        m.content
        for m in model.received[1]
        if isinstance(m, ToolMessage) and isinstance(m.content, str)
    ]
    assert len(knowledge_result) == 1
    assert "구매는 소지금이" in knowledge_result[0]
    # ARTEL-180: the answer carried no scene, and the observation that follows is
    # what puts the first one in the transcript.
    assert SCENE_VIEW_START_PREFIX not in knowledge_result[0]


@pytest.mark.parametrize("field", ["id", "tag", "source", "summary", "description", "score"])
def test_a_hit_missing_a_field_is_still_delivered(field: str) -> None:
    """A dropped frame would leave the waiting tool to time out — the run paying
    twenty seconds for a renamed field. Every field defaults instead."""

    async def run() -> None:
        channel, _, tools, sent = make()
        partial = hit()
        partial.pop(field)

        answer(channel, sent, {"query": "q", "model": "e5", "results": [partial]})
        result = await tools["search_knowledge"].ainvoke(
            {"step": 1, "thought": "확인", "query": "구매 규칙"}
        )

        assert "did not answer in time" not in result

    asyncio.run(run())
