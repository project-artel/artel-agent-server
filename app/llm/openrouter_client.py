from collections.abc import Mapping
from typing import Any

import httpx

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.schemas import LLMRequest, LLMResponse


class LLMClientError(RuntimeError):
    pass


class OpenRouterClient(LLMClient):
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        site_url: str | None = None,
        app_title: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.openrouter_api_key
        self._base_url = (base_url or settings.openrouter_base_url).rstrip("/")
        self._site_url = site_url or settings.openrouter_site_url
        self._app_title = app_title or settings.openrouter_app_title
        self._http_client = http_client or httpx.AsyncClient(timeout=60)
        self._owns_client = http_client is None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if not self._api_key:
            raise LLMClientError("OPENROUTER_API_KEY is required.")

        response = await self._http_client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=request.to_chat_payload(),
        )
        response.raise_for_status()
        data = response.json()
        return self._parse_response(data, fallback_model=request.model)

    async def close(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url
        if self._app_title:
            headers["X-Title"] = self._app_title
        return headers

    def _parse_response(
        self,
        data: Mapping[str, Any],
        fallback_model: str,
    ) -> LLMResponse:
        choices = data.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        return LLMResponse(
            id=data.get("id"),
            model=data.get("model") or fallback_model,
            content=message.get("content") or "",
            usage=data.get("usage"),
            raw=dict(data),
        )
