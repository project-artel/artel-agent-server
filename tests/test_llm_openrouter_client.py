import asyncio
import json

import httpx

from app.llm import LLMMessage, LLMRequest, OpenRouterClient


def test_openrouter_client_sends_chat_completion_payload() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "model": "openrouter/auto",
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    async def run() -> None:
        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport)
        client = OpenRouterClient(
            api_key="test-key",
            base_url="https://openrouter.test/api/v1",
            app_title="Test App",
            http_client=http_client,
        )

        response = await client.complete(
            LLMRequest(
                model="openrouter/auto",
                messages=[LLMMessage(role="user", content="hi")],
                temperature=0.2,
            )
        )

        await http_client.aclose()
        assert response.content == "hello"
        assert response.model == "openrouter/auto"

    asyncio.run(run())

    assert captured["url"] == "https://openrouter.test/api/v1/chat/completions"
    assert captured["body"] == {
        "model": "openrouter/auto",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
    }
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "Bearer test-key"
    assert headers["x-title"] == "Test App"
