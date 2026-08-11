"""What `POST /qa-sessions` promises back about the run it just opened.

Orchestration stores this against the try, and it is the only record of what a
run was executed with once the request is gone. Every assertion here is a field
some later comparison will group by.
"""

import pytest
from fastapi.testclient import TestClient

from app.agents.qa.arch import QA_ARCH_LABEL
from app.llm.models import DEFAULT_MODEL, LLMModel
from app.main import app
from app.prompts import load_prompt, resolve_version
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore
from tests.test_qa_prompt_version import open_request


@pytest.fixture(autouse=True)
def _qa_service():
    """Wire the service the way startup does, minus Redis.

    The app's lifespan builds this against a live Redis, so it is installed here
    directly rather than run: nothing in this file needs a session to survive the
    request that opened it.
    """
    app.state.qa_session_service = QaExecutionService(store=InMemoryQaSessionStore())
    yield
    del app.state.qa_session_service


client = TestClient(app)


def open_session(**extra) -> dict:
    response = client.post("/internal/qa-sessions", json=open_request(**extra))
    assert response.status_code == 200, response.text
    return response.json()


def test_the_response_carries_what_the_run_will_use() -> None:
    body = open_session()
    config = body["run_config"]

    assert body["session_id"]
    assert config["model"] == DEFAULT_MODEL.value
    assert config["provider"] == "openai"
    assert config["agent_arch"] == QA_ARCH_LABEL
    assert len(config["agent_fingerprint"]) == 12
    assert "observe_scene" in config["tools"]
    assert config["temperature"] == 0.2


def test_the_prompt_version_comes_back_resolved() -> None:
    """Not the alias. `null` means "the newest", and a run recorded under that
    name stops being attributable the day a new version directory lands."""
    config = open_session()["run_config"]

    assert config["prompt_version"] == resolve_version("qa_run")


def test_the_prompt_hash_is_the_body_that_was_loaded() -> None:
    """A version directory is a name someone chose; editing `v3` in place would
    otherwise file two different prompts under one bucket."""
    config = open_session(prompt_version="v1")["run_config"]

    assert config["prompt_version"] == "v1"
    assert config["prompt_hashes"]["system"] == load_prompt("qa_run", "system", "v1").body_sha256


def test_a_model_without_reasoning_says_so_rather_than_going_quiet() -> None:
    """`reasoning: null` alone has two meanings — not asked for, and not
    offered. A comparison that cannot tell them apart reads a model's missing
    capability as a choice nobody made."""
    config = open_session(model=LLMModel.gpt_4o.value)["run_config"]

    assert config["reasoning"] is None
    assert config["reasoning_supported"] is False


def test_a_reasoning_model_reports_what_it_was_given() -> None:
    config = open_session(
        model=LLMModel.claude_sonnet_5.value, reasoning={"effort": "high"}
    )["run_config"]

    # Nulls are kept rather than stripped: the record's shape stays the same
    # whichever budget kind a model takes, and `git_sha: null` has to mean "this
    # build did not say" rather than "the key is missing".
    assert config["reasoning"] == {"effort": "high", "max_tokens": None}
    assert config["reasoning_supported"] is True
    assert config["git_sha"] is None and config["image_tag"] is None


def test_omitting_arch_is_todays_structure() -> None:
    """Every existing caller omits it, and none of them may change behaviour."""
    assert open_session()["run_config"] == open_session()["run_config"]

    arch = open_session()["run_config"]["arch"]
    assert arch["tool_calls_per_step"] == 15
    assert arch["deadline_seconds"] == 600.0
    assert arch["vision"] is True


def test_a_requested_structure_comes_back_resolved() -> None:
    body = open_session(arch={"vision": "off", "tool_calls_per_step": 20})
    config = body["run_config"]

    # "off" and "auto" are answered, not echoed.
    assert config["arch"]["vision"] is False
    assert config["arch"]["tool_calls_per_step"] == 20
    assert "capture_screen" not in config["tools"]
    assert config["agent_fingerprint"] != open_session()["run_config"]["agent_fingerprint"]


def test_an_out_of_range_knob_is_refused() -> None:
    response = client.post(
        "/internal/qa-sessions", json=open_request(arch={"tool_calls_per_step": 10_000})
    )

    assert response.status_code == 422


def test_an_unknown_knob_is_refused_rather_than_ignored() -> None:
    """Silently dropping it would record a structure the caller did not ask for."""
    response = client.post(
        "/internal/qa-sessions", json=open_request(arch={"nonsense": 1})
    )

    assert response.status_code == 422


def test_the_response_says_this_run_can_report_citations() -> None:
    """Orchestration confirms a finished run's uncited knowledge rows as
    `cited=false`, and must not do that to a run that had no way to report one.

    `null` (could not report) and `false` (could and did not) are different
    answers, and the only honest way to tell them apart after the fact is a mark
    left on the run at the time. This field is that mark.
    """
    assert open_session()["run_config"]["citation_reporting"] is True
