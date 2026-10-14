"""모델 catalog 를 어디서 가져오고, 가져온 값을 얼마나 믿는가.

catalog 의 원본은 orchestration server 의 `llm_model` 테이블이다. 이 모듈은
``GET {ORCHESTRATION_BASE_URL}/internal/llm-models`` 를 불러 `ModelSpec` 으로 바꾸고
``model_catalog_ttl_seconds`` 동안 cache 한다. 모델 호출마다 network 왕복을 하지 않기
위해서다.

응답에는 꺼진 row 도 들어 있다. 지난 사용량의 단가와 이름을 계속 읽어야 하고,
router 와 compaction 같은 내부 호출은 꺼진 row 도 쓰기 때문이다.

갱신이 실패하면(연결 실패, timeout, 200 이 아닌 응답, 모양이 틀린 body) 마지막으로
받은 catalog 를 한 TTL 더 쓴다. 한 번도 받은 적이 없으면 `ModelCatalogUnavailableError`
를 던진다 — 모델 목록 없이 추측해서 도는 것보다 이유가 적힌 실패가 낫다.
"""

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from app.config import get_settings
from app.llm.models import (
    LLMProvider,
    ModelPricing,
    ModelSpec,
    ReasoningEffort,
    ReasoningKind,
)

logger = logging.getLogger(__name__)

CATALOG_PATH = "/internal/llm-models"

ModelCatalog = Mapping[str, ModelSpec]


class ModelCatalogUnavailableError(RuntimeError):
    """catalog 를 한 번도 받지 못했다.

    `ValueError` 가 아닌 것은 의도다. pydantic 검증 안에서 던져져도 422 로 바뀌지 않아,
    호출자는 "그런 모델이 없다"가 아니라 "catalog 를 못 읽었다"를 본다.
    """


class _CamelRow(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, extra="ignore", frozen=True)


class _ReasoningRow(_CamelRow):
    kind: ReasoningKind
    efforts: list[ReasoningEffort] | None = None
    min_tokens: int | None = None
    max_tokens: int | None = None
    step: int | None = None


class _PricingRow(_CamelRow):
    input_per_mtok: float
    output_per_mtok: float
    cache_write_per_mtok: float
    cache_read_per_mtok: float
    as_of: str
    source: str
    long_context_threshold_tokens: int | None = None
    long_context_input_per_mtok: float | None = None
    long_context_output_per_mtok: float | None = None
    long_context_cache_write_per_mtok: float | None = None
    long_context_cache_read_per_mtok: float | None = None


class _CatalogRow(_CamelRow):
    """`GET /internal/llm-models` 응답 배열의 원소 하나."""

    id: str
    label: str
    provider: LLMProvider
    supports_strict_json: bool
    max_input_tokens: int
    input_modalities: list[str]
    reasoning: _ReasoningRow | None = None
    pricing: _PricingRow | None = None
    enabled: bool = True
    sort_order: int = 0

    def to_spec(self) -> ModelSpec:
        reasoning = self.reasoning
        pricing = self.pricing
        return ModelSpec(
            id=self.id,
            provider=self.provider,
            supports_strict_json=self.supports_strict_json,
            label=self.label,
            max_input_tokens=self.max_input_tokens,
            input_modalities=tuple(self.input_modalities),
            reasoning=reasoning.kind if reasoning else None,
            reasoning_efforts=(
                tuple(reasoning.efforts) if reasoning and reasoning.efforts else None
            ),
            reasoning_min_tokens=reasoning.min_tokens if reasoning else None,
            reasoning_max_tokens=reasoning.max_tokens if reasoning else None,
            reasoning_step=reasoning.step if reasoning else None,
            pricing=(
                ModelPricing(**pricing.model_dump()) if pricing is not None else None
            ),
            enabled=self.enabled,
        )


def parse_model_catalog(body: Any) -> ModelCatalog:
    """응답 body 를 id → `ModelSpec` 으로 바꾼다. 순서는 `sortOrder` 순이다.

    body 가 배열이 아니면 `ValueError` 다. 읽을 수 없는 row 하나는 경고를 남기고
    건너뛴다 — 이 server 가 아직 모르는 provider 나 effort 를 가진 row 하나 때문에
    나머지 모델까지 못 쓰게 되면 안 된다.
    """
    if not isinstance(body, list):
        raise ValueError("model catalog body is not a JSON array")
    rows: list[_CatalogRow] = []
    for raw_row in body:
        try:
            rows.append(_CatalogRow.model_validate(raw_row))
        except ValidationError as error:
            row_id = raw_row.get("id") if isinstance(raw_row, dict) else None
            logger.warning(
                "[model-catalog] skipped row %r: %s", row_id, error.errors()[:1]
            )
    rows.sort(key=lambda row: row.sort_order)
    return MappingProxyType({row.id: row.to_spec() for row in rows})


def load_model_catalog_file(path: Path) -> ModelCatalog:
    """JSON 파일 하나를 catalog 로 읽는다. 테스트와 script 가 쓴다."""
    return parse_model_catalog(json.loads(path.read_text(encoding="utf-8")))


class ModelCatalogProvider:
    """catalog 를 필요할 때 가져온다. 여러 thread 가 함께 써도 된다."""

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
        self._catalog: ModelCatalog | None = None
        self._expires_at: float | None = None

    def get(self) -> ModelCatalog:
        """지금 쓸 catalog. 한 번도 받지 못했으면 `ModelCatalogUnavailableError`."""
        with self._lock:
            now = self._clock()
            if (
                self._catalog is not None
                and self._expires_at is not None
                and now < self._expires_at
            ):
                return self._catalog
            # lock 을 쥔 채 가져온다. 동시에 들어온 호출이 각자 요청을 보내지 않고
            # 요청 하나를 기다린다.
            fetched = self._fetch()
            if fetched is not None:
                self._catalog = fetched
            if self._catalog is None:
                # 실패를 cache 하지 않는다. 배포 직후 orchestration 이 몇 초 늦게 뜨는
                # 경우, 실패를 TTL 동안 들고 있으면 그동안의 요청이 전부 실패한다.
                raise ModelCatalogUnavailableError(self._unavailable_reason())
            # 실패했어도 마지막 catalog 의 만료를 한 TTL 미룬다. 그래야 장애 동안
            # 호출마다 timeout 을 기다리지 않고 TTL 마다 한 번만 기다린다.
            self._expires_at = self._clock() + self._ttl_seconds
            return self._catalog

    def invalidate(self) -> None:
        """다음 `get` 이 orchestration 에 다시 묻게 한다. 받아 둔 catalog 는 남긴다."""
        with self._lock:
            self._expires_at = None

    def _unavailable_reason(self) -> str:
        if not self._base_url:
            return (
                "The model catalog is unavailable: ORCHESTRATION_BASE_URL is not set, "
                f"so {CATALOG_PATH} cannot be fetched."
            )
        return (
            "The model catalog is unavailable: "
            f"{self._base_url}{CATALOG_PATH} has not answered with a catalog yet."
        )

    def _fetch(self) -> ModelCatalog | None:
        if not self._base_url:
            return None
        url = f"{self._base_url}{CATALOG_PATH}"
        try:
            with httpx.Client(
                timeout=self._timeout_seconds, transport=self._transport
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as error:
            logger.warning(
                "[model-catalog] orchestration unreachable: %s", type(error).__name__
            )
            return None

        if response.status_code != httpx.codes.OK:
            logger.warning(
                "[model-catalog] orchestration answered %s", response.status_code
            )
            return None

        try:
            catalog = parse_model_catalog(response.json())
        except ValueError as error:
            logger.warning("[model-catalog] unreadable catalog body: %s", error)
            return None
        if not catalog:
            logger.warning("[model-catalog] orchestration answered an empty catalog")
            return None
        return catalog


class FixedModelCatalogProvider:
    """network 없이 정해진 catalog 를 돌려준다. 테스트가 `install_model_catalog` 로 쓴다."""

    def __init__(self, catalog: ModelCatalog) -> None:
        self._catalog = catalog

    def get(self) -> ModelCatalog:
        return self._catalog

    def invalidate(self) -> None:
        return None


_installed: ModelCatalogProvider | FixedModelCatalogProvider | None = None


def install_model_catalog(catalog: ModelCatalog | None) -> None:
    """process 전체가 `catalog` 를 쓰게 한다. None 이면 orchestration 에서 가져오는
    기본 동작으로 돌아간다. 테스트용이다."""
    global _installed
    _installed = FixedModelCatalogProvider(catalog) if catalog is not None else None


@lru_cache
def get_model_catalog_provider() -> ModelCatalogProvider:
    settings = get_settings()
    return ModelCatalogProvider(
        orchestration_base_url=settings.orchestration_base_url,
        ttl_seconds=settings.model_catalog_ttl_seconds,
        timeout_seconds=settings.model_catalog_fetch_timeout_seconds,
    )


def get_model_catalog() -> ModelCatalog:
    """id → `ModelSpec`, `sortOrder` 순. 꺼진 row 도 들어 있다."""
    provider = _installed or get_model_catalog_provider()
    return provider.get()


def invalidate_model_catalog() -> None:
    """다음 `get_model_catalog` 가 orchestration 에 다시 묻게 한다. 받아 둔 catalog 는 남긴다."""
    provider = _installed or get_model_catalog_provider()
    provider.invalidate()
