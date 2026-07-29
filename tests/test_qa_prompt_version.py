"""A QA run can be pinned to one prompt version, and says which it used.

Comparing two prompts means being able to ask for one of them per run, and being
able to tell afterwards which run got which. Both halves are load-bearing: a
per-run override nobody records is an A/B test with no result.
"""

import asyncio
import logging

import pytest

from app.agents.qa import runner as runner_module
from app.agents.qa.runner import QaRunner
from app.agents.qa.tools import QaRunState
from app.agents.scenario import ScenarioDraft, ScenarioStep
from app.api.qa_sessions import OpenQaSessionRequest
from app.qa.channel import QaRunChannel
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore


def make_scenario() -> ScenarioDraft:
    return ScenarioDraft(
        title="튜토리얼",
        description="튜토리얼 진입을 확인한다",
        steps=[
            ScenarioStep(
                step=1,
                title="시작",
                state="타이틀 화면",
                action="시작 버튼을 누른다",
                expected="튜토리얼 화면으로 넘어간다",
            )
        ],
    )


def open_request(**extra) -> dict:
    return {
        "context": {
            "qa_try_id": 7,
            "game_instance_id": 1,
            "test_scenario_id": 1,
            "scenario": make_scenario().model_dump(),
        },
        **extra,
    }


# --- the API contract ---------------------------------------------------------


def test_a_request_without_a_prompt_version_still_parses() -> None:
    """Every existing caller omits the field; none of them may start failing."""
    request = OpenQaSessionRequest.model_validate(open_request())

    assert request.prompt_version is None


def test_a_request_may_name_a_prompt_version() -> None:
    request = OpenQaSessionRequest.model_validate(open_request(prompt_version="v1"))

    assert request.prompt_version == "v1"


# --- the path from the request to the runner ----------------------------------


class RecordingRunner:
    def __init__(self) -> None:
        self.ran = asyncio.Event()

    async def run_with_deadline(self, channel, scenario):
        self.ran.set()
        return None, None


async def _run_session(prompt_version: str | None) -> str | None:
    seen: list[str | None] = []
    runner = RecordingRunner()

    def factory(model, language, version):
        seen.append(version)
        return runner

    service = QaExecutionService(
        store=InMemoryQaSessionStore(), runner_factory=factory
    )
    session_id = await service.open(
        qa_try_id=7,
        game_instance_id=1,
        test_scenario_id=1,
        scenario=make_scenario(),
        prompt_version=prompt_version,
    )

    async def send(_frame: dict) -> None:
        return None

    await service.run(session_id, send)
    assert runner.ran.is_set()
    return seen[0]


def test_the_requested_version_reaches_the_runner() -> None:
    assert asyncio.run(_run_session("v1")) == "v1"


def test_omitting_the_version_leaves_the_choice_to_the_loader() -> None:
    assert asyncio.run(_run_session(None)) is None


# --- what the run leaves behind -----------------------------------------------


class SilentAgent:
    """An agent that ends the run without emitting anything."""

    def astream(self, *_args, **_kwargs):
        async def updates():
            return
            yield {}  # pragma: no cover - makes this an async generator

        return updates()


@pytest.fixture
def stubbed_agent(monkeypatch):
    monkeypatch.setattr(runner_module, "build_chat_model", lambda model: object())
    monkeypatch.setattr(
        runner_module, "create_agent", lambda **_kwargs: SilentAgent()
    )


def test_the_run_start_log_names_the_prompt_version(stubbed_agent, caplog) -> None:
    """Without it, a trace shows what the model read but not which candidate it was."""

    async def send(_frame: dict) -> None:
        return None

    scenario = make_scenario()
    channel = QaRunChannel(qa_try_id=7, send=send)

    with caplog.at_level(logging.INFO, logger="app.agents.qa.runner"):
        asyncio.run(
            QaRunner(prompt_version="v1").run(
                channel, scenario, QaRunState(total_steps=1)
            )
        )

    starting = [
        record.getMessage()
        for record in caplog.records
        if "[QA] run starting" in record.getMessage()
    ]
    assert len(starting) == 1
    assert "prompt_version=v1" in starting[0]
    # The system prompt still reaches the log, rendered rather than templated.
    assert "You are a QA agent executing an approved test scenario" in starting[0]
    assert "{language_directive}" not in starting[0]
