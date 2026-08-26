import asyncio

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda

from app.agents import (
    AgentContext,
    PhrasedStep,
    PhrasedSteps,
    StepPhrasingAgent,
    StepPhrasingError,
    StepPhrasingRequest,
)
from app.agents.step_phrasing.agent import MAX_STEPS
from app.agents.step_phrasing.prompt import build_chain_inputs

_CTX = AgentContext(session_id="step-phrasing-1")


def _canned(steps: list[PhrasedStep]) -> StepPhrasingAgent:
    result = PhrasedSteps(steps=steps)
    return StepPhrasingAgent(
        structured_factory=lambda model: RunnableLambda(lambda _inputs: result)
    )


def _request(said: str = "대화 끝나면 스페이스 한번 더") -> StepPhrasingRequest:
    return StepPhrasingRequest(
        said=said,
        blocked_by="StoryScene→Map_scene",
        before="StoryScene에서 Space 입력을 한다.",
        after="Map_scene에 진입해 튜토리얼 대화를 확인한다.",
    )


def test_one_sentence_becomes_the_steps_it_describes() -> None:
    agent = _canned([PhrasedStep(action="대화가 끝날 때까지 Space를 누른다.", input="Space")])

    out = asyncio.run(agent.run(_request(), _CTX))

    assert [step.action for step in out] == ["대화가 끝날 때까지 Space를 누른다."]
    assert out[0].input == "Space"


def test_two_actions_in_one_sentence_stay_two_steps() -> None:
    agent = _canned(
        [
            PhrasedStep(action="Space를 눌러 대화를 끝낸다."),
            PhrasedStep(action="시작 버튼을 클릭한다."),
        ]
    )

    out = asyncio.run(agent.run(_request("스페이스로 넘기고 시작 눌러"), _CTX))

    assert [step.action for step in out] == [
        "Space를 눌러 대화를 끝낸다.",
        "시작 버튼을 클릭한다.",
    ]


def test_no_steps_is_an_answer_not_a_failure() -> None:
    # "잘 모르겠는데" is a reply to the question, not a way across. The caller
    # keeps the gap and hands the sentence to the conversation.
    agent = _canned([])

    assert asyncio.run(agent.run(_request("나도 잘 모르겠는데"), _CTX)) == []


def test_blank_input_never_reaches_the_model() -> None:
    def explode(_model):  # pragma: no cover - called only if the guard fails
        raise AssertionError("a blank answer must not cost a model call")

    agent = StepPhrasingAgent(structured_factory=explode)

    assert asyncio.run(agent.run(_request("   "), _CTX)) == []


def test_empty_actions_are_dropped_and_surplus_is_trimmed() -> None:
    agent = _canned(
        [PhrasedStep(action="  ")] + [PhrasedStep(action=f"스텝 {n}") for n in range(20)]
    )

    out = asyncio.run(agent.run(_request(), _CTX))

    assert len(out) == MAX_STEPS
    assert out[0].action == "스텝 0"


def test_unparseable_output_is_a_failure_not_an_empty_answer() -> None:
    # The caller falls back to the user's own sentence on this, which it must not
    # do when the model deliberately returned nothing.
    def failing(_model):
        def raise_parse_error(_inputs):
            raise OutputParserException("not json")

        return RunnableLambda(raise_parse_error)

    agent = StepPhrasingAgent(structured_factory=failing)

    with pytest.raises(StepPhrasingError):
        asyncio.run(agent.run(_request(), _CTX))


def test_missing_neighbours_are_named_rather_than_left_blank() -> None:
    # A bare label invites the model to invent the neighbour it cannot see.
    inputs = build_chain_inputs(
        StepPhrasingRequest(said="스페이스", blocked_by="", before="", after="")
    )

    assert inputs["before"] == "(this gap is at the start)"
    assert inputs["after"] == "(this gap is at the end)"
    assert inputs["blocked_by"] == "(not named)"
