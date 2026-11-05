"""Jev 를 부르는 네 곳이 `app/llm/decision.py` 를 지나 key provider 의 key 로 Jev 를 부르는지 검사한다.

#224 가 `Settings.llm_api_key` 를 지운 뒤에도 Jev 호출 코드는 그 field 를 읽어 매 호출이
`AttributeError` 로 실패했다. 기존 테스트는 `get_settings` 를 `SimpleNamespace` 로 바꿔
없는 field 를 채워 넣었기 때문에 이 실패를 잡지 못했다. 그래서 여기서는 실제
`Settings` 를 쓰고 key provider, 결정 model 설정, HTTP client 만 바꾼다.
"""

import asyncio
from types import SimpleNamespace

import pytest

# app.qa 를 먼저 import 하면 app.agents 와 순환하므로 기존 QA 테스트의 import 순서를 따른다.
import app.agents  # noqa: F401

from app.agents.scenario import router
from app.llm import decision
from app.qa import jev, reach, relevance


class FakeResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"answers": {"answer": {"choice": "yes"}}}


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    @property
    def headers(self) -> list[dict]:
        return [call["headers"] for call in self.calls]

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return FakeResponse()


def _keys(openrouter: str | None, typesafe: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_openrouter_key=lambda: openrouter,
        get_typesafe_key=lambda: typesafe,
        invalidate=lambda: None,
    )


@pytest.fixture(autouse=True)
def _jev_is_the_decision_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        decision,
        "get_model_settings_provider",
        lambda: SimpleNamespace(get_decision_model=lambda: None),
    )


def _call_jev(http: FakeHttp) -> None:
    client = jev.JevClient()
    client._http = http
    asyncio.run(client._post({}))


def _call_relevance(http: FakeHttp) -> None:
    judge = relevance.PulseRelevanceJudge()
    judge._http = http
    asyncio.run(judge._post({}))


def _call_reach(http: FakeHttp) -> None:
    judge = reach.UnreachedJudge()
    judge._http = http
    asyncio.run(judge._post({}))


def _call_router(http: FakeHttp) -> None:
    made = router.JevScenarioRouter()
    made._http = http
    asyncio.run(made._decide("새 시나리오 만들어 줘"))


CALLERS = [
    pytest.param(_call_jev, id="jev"),
    pytest.param(_call_relevance, id="relevance"),
    pytest.param(_call_reach, id="reach"),
    pytest.param(_call_router, id="router"),
]


@pytest.mark.parametrize("call", CALLERS)
def test_the_provider_key_goes_into_the_authorization_header(
    monkeypatch: pytest.MonkeyPatch, call
) -> None:
    monkeypatch.setattr(decision, "get_llm_api_key_provider", lambda: _keys("stored-key"))
    http = FakeHttp()

    call(http)

    assert http.headers[0]["Authorization"] == "Bearer stored-key"
    assert http.calls[0]["url"].endswith("/api/alpha/decisions")
    assert http.calls[0]["json"]["model"] == "typesafe/jev-1.13"


@pytest.mark.parametrize("call", CALLERS)
def test_a_typesafe_key_sends_the_call_to_typesafe_directly(
    monkeypatch: pytest.MonkeyPatch, call
) -> None:
    monkeypatch.setattr(
        decision, "get_llm_api_key_provider", lambda: _keys("openrouter-key", "typesafe-key")
    )
    http = FakeHttp()

    call(http)

    assert http.calls[0]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert http.headers[0]["Authorization"] == "Bearer typesafe-key"
    assert http.calls[0]["json"]["model"] == "jev-1.13.0"


@pytest.mark.parametrize("call", CALLERS)
def test_no_stored_key_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch, call
) -> None:
    monkeypatch.setattr(decision, "get_llm_api_key_provider", lambda: _keys(None))
    http = FakeHttp()

    with pytest.raises(Exception, match="OpenRouter key"):
        call(http)

    assert http.calls == []
