"""다시 해도 안 되는 모델 API 오류에서 런을 멈추고 원인 코드를 보낸다(ARTEL-1370)."""

import asyncio

import anthropic
import httpx
import openai

from app.agents.qa.runner import QaRunner
from app.qa.provider_error import (
    AUTH_FAILED,
    CREDIT_EXHAUSTED,
    MAX_PROVIDER_MESSAGE,
    MODEL_UNAVAILABLE,
    classify_provider_error,
)
from app.qa.schemas import QaRunScenario, QaScenario
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore

CREDIT_TEXT = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing."


def _anthropic(status: int, message: str, error_class=anthropic.APIStatusError):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    body = {"type": "error", "error": {"type": "invalid_request_error", "message": message}}
    response = httpx.Response(status, request=request, json=body)
    return error_class(f"Error code: {status} - {body}", response=response, body=body)


def _openai(status: int, message: str, host="api.openai.com", code=None, error_class=openai.APIStatusError):
    request = httpx.Request("POST", f"https://{host}/v1/chat/completions")
    body = {"message": message, "type": code, "code": code}
    response = httpx.Response(status, request=request, json={"error": body})
    return error_class(f"Error code: {status}", response=response, body=body)


def test_anthropic_credit_balance_is_credit_exhausted() -> None:
    found = classify_provider_error(_anthropic(400, CREDIT_TEXT, anthropic.BadRequestError))
    assert found is not None
    assert (found.code, found.provider) == (CREDIT_EXHAUSTED, "anthropic")
    assert found.provider_message == CREDIT_TEXT
    assert found.summary == "The model API token balance is exhausted, so the run stopped."
    assert "Anthropic" not in found.summary


def test_openrouter_402_and_openai_quota_are_credit_exhausted() -> None:
    openrouter = classify_provider_error(_openai(402, "Insufficient credits", host="openrouter.ai"))
    quota = classify_provider_error(
        _openai(429, "You exceeded your current quota", code="insufficient_quota", error_class=openai.RateLimitError)
    )
    assert (openrouter.code, openrouter.provider) == (CREDIT_EXHAUSTED, "openrouter")
    assert (quota.code, quota.provider) == (CREDIT_EXHAUSTED, "openai")


def test_401_and_403_are_auth_failed() -> None:
    assert classify_provider_error(_anthropic(401, "invalid x-api-key", anthropic.AuthenticationError)).code == AUTH_FAILED
    assert classify_provider_error(_openai(403, "forbidden", error_class=openai.PermissionDeniedError)).code == AUTH_FAILED


def test_missing_key_before_any_call_is_auth_failed() -> None:
    from app.llm.chat_model import ClaudeApiKeyMissingError

    found = classify_provider_error(ClaudeApiKeyMissingError())
    assert (found.code, found.provider) == (AUTH_FAILED, "anthropic")


def test_unknown_model_is_model_unavailable() -> None:
    not_found = classify_provider_error(_anthropic(404, "model: claude-x", anthropic.NotFoundError))
    bad_model = classify_provider_error(_openai(400, "The model `gpt-9` does not exist", error_class=openai.BadRequestError))
    assert (not_found.code, not_found.provider) == (MODEL_UNAVAILABLE, "anthropic")
    assert bad_model.code == MODEL_UNAVAILABLE


def test_a_retired_bedrock_model_is_model_unavailable() -> None:
    """카탈로그에 남은 `bedrock/` 을 고른 런은 다시 해도 같다(ARTEL-1438). 런을 멈춘다."""
    from app.llm.chat_model import RetiredModelError

    found = classify_provider_error(RetiredModelError("Model 'bedrock/x' is on the Bedrock route, which is no longer called."))
    assert (found.code, found.provider) == (MODEL_UNAVAILABLE, "bedrock")


def test_wrapped_error_is_found_through_its_cause() -> None:
    inner = _anthropic(400, CREDIT_TEXT, anthropic.BadRequestError)
    try:
        try:
            raise inner
        except anthropic.BadRequestError as error:
            raise RuntimeError("tool failed") from error
    except RuntimeError as outer:
        assert classify_provider_error(outer).code == CREDIT_EXHAUSTED


def test_retryable_and_our_own_errors_are_left_alone() -> None:
    assert classify_provider_error(_openai(429, "Rate limit reached", error_class=openai.RateLimitError)) is None
    assert classify_provider_error(_anthropic(529, "Overloaded")) is None
    assert classify_provider_error(_anthropic(400, "messages: text content blocks must be non-empty")) is None
    assert classify_provider_error(ValueError("boom")) is None
    assert classify_provider_error(TimeoutError()) is None


def test_long_provider_text_is_clipped() -> None:
    found = classify_provider_error(_anthropic(400, "credit balance " + "x" * 2000))
    assert len(found.provider_message) == MAX_PROVIDER_MESSAGE


def _scenario(title: str) -> QaScenario:
    return QaScenario(title=title, description="d")


def _run(error: BaseException, scenarios: int = 3):
    """[error] 를 첫 시나리오에서 내는 runner 로 런을 돌린다. 보낸 프레임과 runner · 초기화 호출 수를 돌려준다."""
    calls = {"runner": 0, "reset": 0}

    async def raising(self, channel, scenario, state):
        calls["runner"] += 1
        raise error

    class CountingReset:
        async def between_scenarios(self, channel, completed_index, total):
            calls["reset"] += 1

    original = QaRunner.run
    QaRunner.run = raising
    try:
        async def go() -> list[dict]:
            service = QaExecutionService(
                store=InMemoryQaSessionStore(),
                runner_factory=lambda *, config, **_: QaRunner(),
                reset_policy=CountingReset(),
            )
            session_id, _ = await service.open(
                qa_run_id=9,
                game_instance_id=1,
                scenarios=[
                    QaRunScenario(qa_try_id=100 + n, test_scenario_id=n, scenario=_scenario(f"s{n}"))
                    for n in range(1, scenarios + 1)
                ],
            )
            sent: list[dict] = []

            async def send(frame: dict) -> None:
                sent.append(frame)

            await service.run(session_id, send)
            return sent

        return asyncio.run(go()), calls
    finally:
        QaRunner.run = original


def test_provider_error_stops_the_run_at_the_first_scenario() -> None:
    sent, calls = _run(_anthropic(400, CREDIT_TEXT, anthropic.BadRequestError))

    assert calls == {"runner": 1, "reset": 0}
    assert {frame["qaTryId"] for frame in sent} == {"101"}
    error = next(frame["payload"] for frame in sent if frame["type"] == "ERROR")
    assert error["code"] == CREDIT_EXHAUSTED
    assert error["retryable"] is False
    assert error["detail"] == {"provider": "anthropic", "provider_message": CREDIT_TEXT}
    terminal = [frame["payload"] for frame in sent if frame["type"] == "STATUS"]
    assert len(terminal) == 1
    assert terminal[0]["status"] == "FAILED"
    assert terminal[0]["result"] == "UNDECIDED"
    assert terminal[0]["stop"] == {
        "code": CREDIT_EXHAUSTED,
        "provider": "anthropic",
        "message": "The model API token balance is exhausted, so the run stopped.",
    }


def test_other_errors_still_move_on_to_the_next_scenario() -> None:
    sent, calls = _run(ValueError("boom"))

    assert calls == {"runner": 3, "reset": 2}
    codes = [frame["payload"]["code"] for frame in sent if frame["type"] == "ERROR"]
    assert codes == ["run_incomplete"] * 3
    results = [frame["payload"]["result"] for frame in sent if frame["type"] == "STATUS"]
    assert results == ["FAILED"] * 3
