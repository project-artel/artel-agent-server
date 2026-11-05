"""기능별 model 설정을 orchestration 에서 읽는 provider. key provider 와 같은 TTL 과 실패 처리를 쓴다."""

import httpx
from fastapi.testclient import TestClient

from app.llm.model_settings import ModelSettingsProvider
from app.main import app

BASE_URL = "http://orchestration:8081"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build_provider(handler, *, ttl=30.0, clock=None, base_url=BASE_URL):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    provider = ModelSettingsProvider(
        orchestration_base_url=base_url,
        ttl_seconds=ttl,
        transport=httpx.MockTransport(recording),
        clock=clock or FakeClock(),
    )
    return provider, calls


def answer(decision=None, authoring=None):
    return lambda request: httpx.Response(
        200, json={"scenarioAuthoringModel": authoring, "decisionModel": decision}
    )


def test_the_decision_model_is_read_from_the_settings_endpoint():
    provider, calls = build_provider(answer("openai/gpt-6-luna", "anthropic/claude-sonnet-5.5"))

    assert provider.get_decision_model() == "openai/gpt-6-luna"
    assert calls[0].url.path == "/internal/settings/models"


def test_null_and_blank_mean_no_model():
    provider, _ = build_provider(answer(None))
    assert provider.get_decision_model() is None

    blank, _ = build_provider(answer("  "))
    assert blank.get_decision_model() is None


def test_no_orchestration_url_means_no_model_and_no_request():
    provider, calls = build_provider(answer("openai/gpt-6-luna"), base_url=None)

    assert provider.get_decision_model() is None
    assert calls == []


def test_the_value_is_cached_for_the_ttl():
    clock = FakeClock()
    values = iter(["first/model", "second/model"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"decisionModel": next(values)}),
        ttl=30.0,
        clock=clock,
    )

    assert provider.get_decision_model() == "first/model"
    clock.now += 29
    assert provider.get_decision_model() == "first/model"
    assert len(calls) == 1

    clock.now += 2
    assert provider.get_decision_model() == "second/model"
    assert len(calls) == 2


def test_invalidate_makes_the_next_call_ask_again():
    values = iter(["first/model", "second/model"])
    provider, calls = build_provider(
        lambda request: httpx.Response(200, json={"decisionModel": next(values)})
    )

    assert provider.get_decision_model() == "first/model"
    provider.invalidate()

    assert provider.get_decision_model() == "second/model"
    assert len(calls) == 2


def test_a_failed_fetch_keeps_the_last_value():
    answers = iter(
        [
            httpx.Response(200, json={"decisionModel": "first/model"}),
            httpx.Response(500),
            httpx.Response(200, content=b"not json"),
        ]
    )
    provider, calls = build_provider(lambda request: next(answers))

    assert provider.get_decision_model() == "first/model"
    provider.invalidate()
    assert provider.get_decision_model() == "first/model"
    provider.invalidate()
    assert provider.get_decision_model() == "first/model"
    assert len(calls) == 3


def test_an_unreachable_orchestration_before_any_answer_means_no_model():
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    provider, calls = build_provider(unreachable)

    assert provider.get_decision_model() is None
    # 실패도 TTL 하나만큼 기억한다. 장애 동안 호출마다 기다리지 않게 하려는 것이다.
    assert provider.get_decision_model() is None
    assert len(calls) == 1


def test_the_invalidate_endpoint_drops_the_model_settings(monkeypatch):
    values = iter(["first/model", "second/model"])
    provider, _ = build_provider(
        lambda request: httpx.Response(200, json={"decisionModel": next(values)}),
        ttl=3600,
    )
    monkeypatch.setattr("app.api.llm_cache.get_model_settings_provider", lambda: provider)
    assert provider.get_decision_model() == "first/model"

    response = TestClient(app).post("/internal/llm-cache/invalidate")

    assert response.status_code == 204
    assert provider.get_decision_model() == "second/model"
