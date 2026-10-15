"""Jev client의 요청 형식과 실패 처리를 네트워크 없이 검사한다."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

# app.qa를 먼저 import하면 app.agents와 순환하므로 기존 QA 테스트의 import 순서를 따른다.
import app.agents  # noqa: F401

from app.qa import jev
from app.qa.jev import JevClient, JevError


def test_choose_sends_exact_request_and_returns_option(caplog: pytest.LogCaptureFixture) -> None:
    bodies: list[dict] = []

    async def post(body: dict) -> dict:
        bodies.append(body)
        return {"answers": {"answer": {"choice": "heal"}}, "usage": {"cost": 0.001}}

    with caplog.at_level("INFO", logger=jev.__name__):
        chosen = asyncio.run(
            JevClient(post).choose(
                "Choose the next action.",
                {"attack": "Deal damage.", "heal": "Recover health."},
                {"scene": "Battle", "hp": 3},
            )
        )

    assert chosen == "heal"
    assert bodies == [
        {
            "model": "typesafe/jev-1.13",
            "state": {"scene": "Battle", "hp": 3},
            "questions": {
                "answer": {
                    "type": "choice",
                    "instructions": "Choose the next action.",
                    "criteria": {
                        "attack": {"what": "Deal damage."},
                        "heal": {"what": "Recover health."},
                    },
                }
            },
        }
    ]
    assert "Jev cost $0.001000" in caplog.text


def test_probability_sends_exact_request_and_returns_float() -> None:
    bodies: list[dict] = []

    async def post(body: dict) -> dict:
        bodies.append(body)
        return {"answers": {"answer": {"noul": 0.83}}}

    probability = asyncio.run(
        JevClient(post).probability(
            "Can the player continue?",
            {"scene": "Battle", "alive": True},
            "The player can act.",
            "The battle has ended.",
        )
    )

    assert probability == 0.83
    assert bodies == [
        {
            "model": "typesafe/jev-1.13",
            "state": {"scene": "Battle", "alive": True},
            "questions": {
                "answer": {
                    "type": "noul",
                    "instructions": "Can the player continue?",
                    "criteria": {
                        "true": "The player can act.",
                        "false": "The battle has ended.",
                    },
                }
            },
        }
    ]


def test_missing_api_key_raises_jev_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        jev, "get_llm_api_key_provider", lambda: SimpleNamespace(get_openrouter_key=lambda: None)
    )

    with pytest.raises(JevError, match="OpenRouter key"):
        asyncio.run(JevClient().choose("Choose.", {"yes": "Yes."}, {}))


def test_default_post_uses_settings_and_reuses_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        jev,
        "get_settings",
        lambda: SimpleNamespace(
            llm_base_url="https://example.com/api/v1/",
            openrouter_site_url="https://artel.example",
            openrouter_app_title="Artel",
        ),
    )
    monkeypatch.setattr(
        jev, "get_llm_api_key_provider", lambda: SimpleNamespace(get_openrouter_key=lambda: "secret")
    )
    clients: list[object] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"answers": {"answer": {"choice": "yes"}}}

    class FakeAsyncClient:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout
            self.calls: list[tuple[str, dict, dict]] = []
            self.closed = False
            clients.append(self)

        async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
            self.calls.append((url, headers, json))
            return FakeResponse()

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(jev.httpx, "AsyncClient", FakeAsyncClient)

    async def run_calls() -> None:
        client = JevClient()
        assert await client.choose("Choose.", {"yes": "Yes."}, {}) == "yes"
        assert await client.choose("Choose again.", {"yes": "Yes."}, {}) == "yes"
        await client.aclose()

    asyncio.run(run_calls())

    assert len(clients) == 1
    http_client = clients[0]
    assert http_client.timeout == 20.0
    assert http_client.closed
    assert len(http_client.calls) == 2
    assert http_client.calls[0] == (
        "https://example.com/api/alpha/decisions",
        {
            "Authorization": "Bearer secret",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://artel.example",
            "X-Title": "Artel",
        },
        {
            "model": "typesafe/jev-1.13",
            "state": {},
            "questions": {
                "answer": {
                    "type": "choice",
                    "instructions": "Choose.",
                    "criteria": {"yes": {"what": "Yes."}},
                }
            },
        },
    )


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (httpx.ConnectError("connection refused"), "HTTP request failed"),
        (httpx.ReadTimeout("read timed out"), "timed out"),
        (asyncio.TimeoutError(), "timed out"),
        (
            httpx.HTTPStatusError(
                "bad status",
                request=httpx.Request("POST", "https://example.com/api/alpha/decisions"),
                response=httpx.Response(503),
            ),
            "HTTP 503",
        ),
        (ValueError("invalid JSON"), "request failed"),
    ],
)
def test_post_failures_raise_jev_error(failure: Exception, reason: str) -> None:
    async def post(body: dict) -> dict:
        raise failure

    with pytest.raises(JevError, match=reason):
        asyncio.run(JevClient(post).choose("Choose.", {"yes": "Yes."}, {}))


@pytest.mark.parametrize("response", [{}, {"answers": {}}, {"answers": {"answer": {}}}])
def test_missing_choice_answer_raises_jev_error(response: dict) -> None:
    async def post(body: dict) -> dict:
        return response

    with pytest.raises(JevError, match="answers.answer.choice"):
        asyncio.run(JevClient(post).choose("Choose.", {"yes": "Yes."}, {}))


@pytest.mark.parametrize("response", [{}, {"answers": {}}, {"answers": {"answer": {}}}])
def test_missing_noul_answer_raises_jev_error(response: dict) -> None:
    async def post(body: dict) -> dict:
        return response

    with pytest.raises(JevError, match="noul response"):
        asyncio.run(JevClient(post).probability("Ask.", {}, "Yes.", "No."))


def test_unknown_choice_raises_jev_error() -> None:
    async def post(body: dict) -> dict:
        return {"answers": {"answer": {"choice": "unknown"}}}

    with pytest.raises(JevError, match="unknown option"):
        asyncio.run(JevClient(post).choose("Choose.", {"yes": "Yes."}, {}))


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan")])
def test_out_of_range_noul_raises_jev_error(value: float) -> None:
    async def post(body: dict) -> dict:
        return {"answers": {"answer": {"noul": value}}}

    with pytest.raises(JevError, match="probability in \\[0, 1\\]"):
        asyncio.run(JevClient(post).probability("Ask.", {}, "Yes.", "No."))


def test_noul_range_includes_endpoints() -> None:
    async def post(body: dict) -> dict:
        return {"answers": {"answer": {"noul": 0.0}}}

    assert asyncio.run(JevClient(post).probability("Ask.", {}, "Yes.", "No.")) == 0.0


def test_cancelled_error_passes_through() -> None:
    async def post(body: dict) -> dict:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(JevClient(post).choose("Choose.", {"yes": "Yes."}, {}))
