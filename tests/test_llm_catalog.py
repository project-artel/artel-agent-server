"""The model catalog client: fetch, TTL, the last good catalog on failure, parsing."""

import json

import httpx
import pytest

import app.llm.catalog as catalog_module
from app.llm.catalog import (
    CATALOG_PATH,
    ModelCatalogProvider,
    ModelCatalogUnavailableError,
    parse_model_catalog,
)
from app.llm.models import (
    LLMProvider,
    ModelPricing,
    ReasoningEffort,
    ReasoningKind,
    get_model_spec,
)
from app.llm.usage import _estimate_cost
from tests.conftest import MODEL_CATALOG_FIXTURE

BASE_URL = "http://orchestration:8081"

SEED_IDS = [
    "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "openai/gpt-6.1-sol",
    "openai/gpt-6-astra",
    "openai/gpt-6-luna",
    "openai/gpt-5.6-terra",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "google/gemini-3.8-flash",
    "google/gemma-4-31b-it:free",
    "x-ai/grok-4.7",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.3-flash",
    "qwen/qwen3.7-max",
    "bedrock/us.anthropic.claude-haiku-5-5",
    "bedrock/us.openai.gpt-6.1-sol",
    "claude/claude-opus-5-5",
    "claude/claude-sonnet-5-5",
    "claude/claude-haiku-5-5",
    "claude/claude-fable-5-1",
]

CLAUDE_IDS = SEED_IDS[-4:]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fixture_rows() -> list[dict]:
    return json.loads(MODEL_CATALOG_FIXTURE.read_text(encoding="utf-8"))


def build_provider(handler, *, ttl=30.0, clock=None, base_url=BASE_URL):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    provider = ModelCatalogProvider(
        orchestration_base_url=base_url,
        ttl_seconds=ttl,
        transport=httpx.MockTransport(recording),
        clock=clock or FakeClock(),
    )
    return provider, calls


def answer(rows):
    return lambda request: httpx.Response(200, json=rows)


# --- the fixture mirrors the seed ----------------------------------------------


def test_fixture_mirrors_the_nineteen_seed_rows_in_order() -> None:
    rows = fixture_rows()

    assert [row["id"] for row in rows] == SEED_IDS
    assert [row["sortOrder"] for row in rows] == list(range(10, 200, 10))
    disabled = [row["id"] for row in rows if not row["enabled"]]
    assert disabled == [
        "bedrock/us.anthropic.claude-haiku-5-5",
        "bedrock/us.openai.gpt-6.1-sol",
        *CLAUDE_IDS,
    ]


def test_claude_api_rows_parse_with_effort_reasoning_and_pricing() -> None:
    catalog = parse_model_catalog(fixture_rows())

    for model_id in CLAUDE_IDS:
        spec = catalog[model_id]
        assert spec.provider is LLMProvider.anthropic
        assert spec.enabled is False
        assert spec.supports_strict_json is True
        assert spec.max_input_tokens == 872_000
        assert spec.input_modalities == ("text", "image", "file")
        assert spec.reasoning is ReasoningKind.effort
        assert spec.reasoning_efforts == tuple(ReasoningEffort)
        assert spec.pricing is not None
        assert spec.pricing.as_of == "2026-10-14"
        assert spec.pricing.source == (
            "Anthropic model table (claude-api skill, cached 2026-10-06)"
        )

    prices = {
        model_id: (
            catalog[model_id].pricing.input_per_mtok,
            catalog[model_id].pricing.output_per_mtok,
            catalog[model_id].pricing.cache_write_per_mtok,
            catalog[model_id].pricing.cache_read_per_mtok,
        )
        for model_id in CLAUDE_IDS
    }
    assert prices == {
        "claude/claude-opus-5-5": (4.0, 20.0, 5.0, 0.2),
        "claude/claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.2),
        "claude/claude-haiku-5-5": (0.1, 0.5, 0.125, 0.01),
        "claude/claude-fable-5-1": (10.0, 50.0, 12.5, 0.25),
    }
    haiku = catalog["claude/claude-haiku-5-5"].pricing
    assert haiku.long_context_threshold_tokens == 100_000
    assert (
        haiku.long_context_input_per_mtok,
        haiku.long_context_output_per_mtok,
        haiku.long_context_cache_write_per_mtok,
        haiku.long_context_cache_read_per_mtok,
    ) == (0.5, 2.5, 0.625, 0.05)
    assert catalog["claude/claude-opus-5-5"].pricing.long_context_threshold_tokens is None


# --- parsing --------------------------------------------------------------------


def test_parsing_builds_specs_with_id_enabled_reasoning_and_pricing() -> None:
    catalog = parse_model_catalog(fixture_rows())

    haiku = catalog["bedrock/us.anthropic.claude-haiku-5-5"]
    assert haiku.id == "bedrock/us.anthropic.claude-haiku-5-5"
    assert haiku.enabled is False
    assert haiku.provider is LLMProvider.anthropic
    assert haiku.supports_strict_json is False
    assert haiku.max_input_tokens == 872_000
    assert haiku.input_modalities == ("text", "image")
    assert haiku.reasoning is ReasoningKind.effort
    assert haiku.reasoning_efforts == tuple(ReasoningEffort)
    assert haiku.pricing == ModelPricing(
        input_per_mtok=0.10,
        output_per_mtok=0.50,
        cache_write_per_mtok=0.125,
        cache_read_per_mtok=0.01,
        as_of="2026-10-13",
        source="Anthropic Claude Haiku 5.5 overview",
        long_context_threshold_tokens=100_000,
        long_context_input_per_mtok=0.50,
        long_context_output_per_mtok=2.50,
        long_context_cache_write_per_mtok=0.625,
        long_context_cache_read_per_mtok=0.05,
    )

    assert haiku.reasoning_default_tokens is None

    sol = catalog["bedrock/us.openai.gpt-6.1-sol"]
    assert sol.provider is LLMProvider.openai
    assert sol.max_input_tokens == 922_000
    assert sol.input_modalities == ("text", "image", "file")
    assert sol.pricing is None


def test_a_token_budget_row_parses_its_default_budget() -> None:
    """Haiku 4.5 reasons on a token budget and has no provider default, so the
    catalog's `reasoning.defaultTokens` is what turns reasoning on when a request
    leaves it unset."""
    haiku = parse_model_catalog(fixture_rows())["bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"]

    assert haiku.enabled is True
    assert haiku.supports_strict_json is True
    assert haiku.max_input_tokens == 183_616
    assert haiku.reasoning is ReasoningKind.max_tokens
    assert haiku.reasoning_efforts is None
    assert haiku.reasoning_min_tokens == 1_024
    assert haiku.reasoning_max_tokens == 32_000
    assert haiku.reasoning_default_tokens == 4_096
    assert haiku.pricing == ModelPricing(
        input_per_mtok=1.00,
        output_per_mtok=5.00,
        cache_write_per_mtok=1.25,
        cache_read_per_mtok=0.10,
        as_of="2026-09-03",
        source="AWS 콘솔, Claude Haiku 4.5 / us-west-2 / on-demand",
    )


def test_null_reasoning_and_null_pricing_parse_as_none() -> None:
    qwen = parse_model_catalog(fixture_rows())["qwen/qwen3.7-max"]

    assert qwen.reasoning is None
    assert qwen.reasoning_efforts is None
    assert qwen.reasoning_min_tokens is None
    assert qwen.reasoning_default_tokens is None
    assert qwen.pricing is None
    assert qwen.enabled is True


def test_parsing_orders_by_sort_order_and_skips_an_unreadable_row() -> None:
    rows = fixture_rows()[:2]
    rows.reverse()
    rows.append({**rows[0], "id": "new/provider", "provider": "unknown-author"})

    catalog = parse_model_catalog(rows)

    assert list(catalog) == ["bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0", "openai/gpt-6.1-sol"]


def test_a_body_that_is_not_an_array_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_model_catalog({"items": []})


# --- fetching and the TTL -------------------------------------------------------


def test_fetches_the_internal_catalog_path() -> None:
    provider, calls = build_provider(answer(fixture_rows()))

    catalog = provider.get()

    assert list(catalog) == SEED_IDS
    assert calls[0].url.path == CATALOG_PATH == "/internal/llm-models"


def test_catalog_is_cached_within_the_ttl_and_refetched_after_it() -> None:
    clock = FakeClock()
    bodies = iter([fixture_rows()[:1], fixture_rows()[:2]])
    provider, calls = build_provider(lambda request: httpx.Response(200, json=next(bodies)), clock=clock)

    assert list(provider.get()) == SEED_IDS[:1]
    clock.now += 29
    assert list(provider.get()) == SEED_IDS[:1]
    assert len(calls) == 1

    clock.now += 2
    assert list(provider.get()) == SEED_IDS[:2]
    assert len(calls) == 2


def test_last_good_catalog_answers_when_a_refresh_fails() -> None:
    clock = FakeClock()
    responses = iter(
        [
            httpx.Response(200, json=fixture_rows()),
            httpx.Response(500),
            httpx.Response(200, json=fixture_rows()[:1]),
        ]
    )
    provider, calls = build_provider(lambda request: next(responses), clock=clock)
    first = provider.get()

    clock.now += 31
    assert provider.get() is first
    # 실패 뒤에도 TTL 하나 동안은 다시 묻지 않는다. 장애 동안 호출마다 timeout 을
    # 기다리지 않기 위해서다.
    clock.now += 29
    assert provider.get() is first
    assert len(calls) == 2

    clock.now += 2
    assert list(provider.get()) == SEED_IDS[:1]


@pytest.mark.parametrize(
    "failure",
    [
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down")),
        lambda request: httpx.Response(503),
        lambda request: httpx.Response(200, content=b"not json"),
        lambda request: httpx.Response(200, json={"not": "a list"}),
        lambda request: httpx.Response(200, json=[]),
    ],
)
def test_last_good_catalog_survives_every_kind_of_failed_refresh(failure) -> None:
    clock = FakeClock()
    answers = iter([answer(fixture_rows()), failure])
    provider, _ = build_provider(lambda request: next(answers)(request), clock=clock)
    first = provider.get()

    clock.now += 31

    assert provider.get() is first


def test_never_loaded_catalog_raises_a_dedicated_error_and_retries_next_call() -> None:
    responses = iter([httpx.Response(503), httpx.Response(200, json=fixture_rows())])
    provider, calls = build_provider(lambda request: next(responses))

    with pytest.raises(ModelCatalogUnavailableError, match="/internal/llm-models"):
        provider.get()
    # 한 번도 못 받았을 때는 실패를 cache 하지 않는다. orchestration 이 늦게 떠도
    # 바로 다음 호출이 catalog 를 받는다.
    assert list(provider.get()) == SEED_IDS
    assert len(calls) == 2


def test_missing_base_url_raises_and_names_the_setting() -> None:
    provider, calls = build_provider(answer(fixture_rows()), base_url=None)

    with pytest.raises(ModelCatalogUnavailableError, match="ORCHESTRATION_BASE_URL"):
        provider.get()
    assert calls == []


def test_unavailable_catalog_is_not_a_value_error() -> None:
    """요청 검증 안에서 던져져도 422("그런 모델 없음")로 바뀌지 않아야 한다."""
    assert not issubclass(ModelCatalogUnavailableError, ValueError)


def test_invalidate_makes_the_next_get_ask_again() -> None:
    provider, calls = build_provider(answer(fixture_rows()))
    provider.get()

    provider.invalidate()
    provider.get()

    assert len(calls) == 2


# --- callers ---------------------------------------------------------------------


def test_get_model_spec_reads_the_installed_catalog() -> None:
    assert get_model_spec("openai/gpt-6-luna").label == "GPT-6 Luna"
    with pytest.raises(ValueError, match="not in the catalog"):
        get_model_spec("openai/not-a-model")


def test_usage_pricing_never_raises_when_the_catalog_is_unavailable(monkeypatch) -> None:
    class Unavailable:
        def get(self):
            raise ModelCatalogUnavailableError("down")

    monkeypatch.setattr(catalog_module, "_installed", Unavailable())

    cost = _estimate_cost(
        "bedrock/us.anthropic.claude-haiku-5-5",
        input_tokens=1000,
        output_tokens=10,
        cached_input_tokens=0,
        cache_write_tokens=0,
    )

    assert cost is None


def test_usage_pricing_is_empty_for_an_id_outside_the_catalog() -> None:
    assert (
        _estimate_cost(
            "someone/else",
            input_tokens=1000,
            output_tokens=10,
            cached_input_tokens=0,
            cache_write_tokens=0,
        )
        is None
    )
