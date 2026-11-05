"""`POST /internal/llm-cache/invalidate` drops the cached keys and the catalog's freshness."""

import httpx
from fastapi.testclient import TestClient

from app.llm import catalog as catalog_module
from app.llm.api_key import LlmApiKeyProvider
from app.llm.catalog import ModelCatalogProvider, install_model_catalog
from app.main import app

BASE_URL = "http://orchestration:8081"

CATALOG_ROW = {
    "id": "openai/gpt-6-luna",
    "label": "GPT-6 Luna",
    "provider": "openai",
    "supportsStrictJson": True,
    "maxInputTokens": 1000,
    "inputModalities": ["text"],
    "reasoning": None,
    "pricing": None,
    "enabled": True,
    "sortOrder": 10,
}


def test_invalidate_drops_both_caches(monkeypatch):
    requests: list[str] = []
    key_answers = iter(["old-key", "new-key"])

    def orchestration(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/internal/settings/llm":
            return httpx.Response(
                200, json={"openrouterApiKey": next(key_answers), "bedrockApiKey": None}
            )
        return httpx.Response(200, json=[CATALOG_ROW])

    transport = httpx.MockTransport(orchestration)
    keys = LlmApiKeyProvider(
        orchestration_base_url=BASE_URL, ttl_seconds=3600, transport=transport
    )
    catalog = ModelCatalogProvider(
        orchestration_base_url=BASE_URL, ttl_seconds=3600, transport=transport
    )
    monkeypatch.setattr("app.api.llm_cache.get_llm_api_key_provider", lambda: keys)
    monkeypatch.setattr(catalog_module, "get_model_catalog_provider", lambda: catalog)
    # The suite installs a fixed catalog; this test needs the fetching provider.
    install_model_catalog(None)

    assert keys.get_openrouter_key() == "old-key"
    catalog_module.get_model_catalog()
    keys.get_openrouter_key()
    catalog_module.get_model_catalog()
    assert len(requests) == 2

    response = TestClient(app).post("/internal/llm-cache/invalidate")

    assert response.status_code == 204
    assert response.content == b""
    assert keys.get_openrouter_key() == "new-key"
    catalog_module.get_model_catalog()
    assert requests == [
        "/internal/settings/llm",
        "/internal/llm-models",
        "/internal/settings/llm",
        "/internal/llm-models",
    ]
