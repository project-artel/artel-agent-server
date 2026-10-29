"""관리자가 기능별로 고른 model 을 orchestration 에서 읽는다.

``GET {ORCHESTRATION_BASE_URL}/internal/settings/models`` 는
``{"scenarioAuthoringModel": ..., "decisionModel": ...}`` 를 돌려주고 두 값 모두 null 일 수
있다. 이 server 가 읽는 것은 ``decisionModel`` 이다. 시나리오 저작 model 은 orchestration 이
세션을 열 때 정해서 보내 주므로 여기서는 읽기만 하고 쓰지 않는다.

cache 와 실패 처리는 `app/llm/api_key.py` 의 `LlmApiKeyProvider` 와 같다.
``llm_key_cache_ttl_seconds`` 동안 믿고, ``POST /internal/llm-cache/invalidate`` 가 오면
다음 호출에서 다시 묻는다. orchestration 이 응답하지 않거나 읽을 수 없는 body 를 주면 마지막
값을 TTL 하나만큼 더 쓴다. 한 번도 읽지 못했으면 null 이고, null 은 Jev 다.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from app.config import get_settings

logger = logging.getLogger(__name__)

MODEL_SETTINGS_PATH = "/internal/settings/models"


class _ModelSettingsBody(BaseModel):
    """The body of `GET /internal/settings/models`."""

    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)

    scenario_authoring_model: str | None = None
    decision_model: str | None = None


@dataclass(frozen=True)
class FeatureModels:
    scenario_authoring: str | None = None
    decision: str | None = None


NO_MODELS = FeatureModels()


def _clean(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


class ModelSettingsProvider:
    """기능별 model 설정을 필요할 때 읽는다. thread 사이에 나눠 써도 된다."""

    def __init__(
        self,
        *,
        orchestration_base_url: str | None,
        ttl_seconds: float,
        timeout_seconds: float = 3.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = (orchestration_base_url or "").rstrip("/")
        self._ttl_seconds = ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._models = NO_MODELS
        self._expires_at: float | None = None

    def get_decision_model(self) -> str | None:
        """Jev 질문에 답할 model id. null 이면 Jev 다."""
        return self._current().decision

    def invalidate(self) -> None:
        """다음 호출이 orchestration 에 다시 묻게 한다. 값은 남겨 두어 재조회가 실패해도 쓴다."""
        with self._lock:
            self._expires_at = None

    def _current(self) -> FeatureModels:
        if not self._base_url:
            return NO_MODELS
        with self._lock:
            now = self._clock()
            if self._expires_at is not None and now < self._expires_at:
                return self._models
            fetched = self._fetch()
            if fetched is not None:
                self._models = fetched
            self._expires_at = self._clock() + self._ttl_seconds
            return self._models

    def _fetch(self) -> FeatureModels | None:
        url = f"{self._base_url}{MODEL_SETTINGS_PATH}"
        try:
            with httpx.Client(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as error:
            logger.warning(
                "[model-settings] orchestration unreachable, keeping the last models: %s",
                type(error).__name__,
            )
            return None

        if response.status_code != httpx.codes.OK:
            logger.warning(
                "[model-settings] orchestration answered %s, keeping the last models",
                response.status_code,
            )
            return None

        try:
            body = _ModelSettingsBody.model_validate_json(response.content)
        except ValidationError:
            logger.warning("[model-settings] orchestration answered an unreadable body")
            return None

        return FeatureModels(
            scenario_authoring=_clean(body.scenario_authoring_model),
            decision=_clean(body.decision_model),
        )


@lru_cache
def get_model_settings_provider() -> ModelSettingsProvider:
    settings = get_settings()
    return ModelSettingsProvider(
        orchestration_base_url=settings.orchestration_base_url,
        ttl_seconds=settings.llm_key_cache_ttl_seconds,
        timeout_seconds=settings.llm_key_fetch_timeout_seconds,
    )
