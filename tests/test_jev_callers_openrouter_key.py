"""Jev 를 직접 부르는 네 곳이 OpenRouter key 를 key provider 에서 읽는지 검사한다.

#224 가 `Settings.llm_api_key` 를 지운 뒤에도 Jev 호출 코드는 그 field 를 읽어 매 호출이
`AttributeError` 로 실패했다. 기존 테스트는 `get_settings` 를 `SimpleNamespace` 로 바꿔
없는 field 를 채워 넣었기 때문에 이 실패를 잡지 못했다. 그래서 여기서는 실제
`Settings` 를 쓰고 key provider 와 HTTP client 만 바꾼다.
"""

import asyncio
from types import SimpleNamespace

import pytest

# app.qa 를 먼저 import 하면 app.agents 와 순환하므로 기존 QA 테스트의 import 순서를 따른다.
import app.agents  # noqa: F401

from app.agents.scenario import router
from app.qa import jev, reach, relevance


class FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"answers": {"answer": {"choice": "yes"}}}


class FakeHttp:
    def __init__(self) -> None:
        self.headers: list[dict] = []

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.headers.append(headers)
        return FakeResponse()


def _keys(key: str | None) -> SimpleNamespace:
    return SimpleNamespace(get_openrouter_key=lambda: key)


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
    pytest.param(jev, _call_jev, id="jev"),
    pytest.param(relevance, _call_relevance, id="relevance"),
    # reach 는 relevance 의 `post_decision` 을 쓰므로 key provider 도 relevance 에서 바꾼다.
    pytest.param(relevance, _call_reach, id="reach"),
    pytest.param(router, _call_router, id="router"),
]


@pytest.mark.parametrize(("module", "call"), CALLERS)
def test_the_provider_key_goes_into_the_authorization_header(
    monkeypatch: pytest.MonkeyPatch, module, call
) -> None:
    monkeypatch.setattr(module, "get_llm_api_key_provider", lambda: _keys("stored-key"))
    http = FakeHttp()

    call(http)

    assert http.headers[0]["Authorization"] == "Bearer stored-key"


@pytest.mark.parametrize(("module", "call"), CALLERS)
def test_no_stored_key_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch, module, call
) -> None:
    monkeypatch.setattr(module, "get_llm_api_key_provider", lambda: _keys(None))
    http = FakeHttp()

    with pytest.raises(Exception, match="OpenRouter key"):
        call(http)

    assert http.headers == []
