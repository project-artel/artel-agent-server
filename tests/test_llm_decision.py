"""결정 질문을 누가 답하는지 고르는 일과, 범용 LLM 이 답할 때의 검사.

chat model 은 쓰지 않는다. `structured` 를 바꿔 끼워 schema 를 받은 그대로 model 의 답을
검사하게 한다. langchain 의 structured output 도 같은 schema 로 답을 검사한다.
"""

import asyncio
from types import SimpleNamespace

import pytest

# app.qa 를 먼저 import 하면 app.agents 와 순환하므로 기존 QA 테스트의 import 순서를 따른다.
import app.agents  # noqa: F401

from app.llm import chat_model, decision
from app.llm.decision import DecisionError
from app.qa import reach, relevance
from app.qa.jev import JevClient, JevError

CHOICE_BODY = {
    "model": "typesafe/jev-1.13",
    "state": {"user_message": "새 시나리오 만들어 줘"},
    "questions": {
        "route": {
            "type": "choice",
            "instructions": "Which branch?",
            "criteria": {
                "authoring": {"what": "make scenarios", "examples": ["짜줘"]},
                "question": {"what": "ask about the run"},
            },
        }
    },
}

NOUL_BODY = {
    "model": "typesafe/jev-1.13",
    "state": {"scene": "Battle"},
    "questions": {
        "m0": {
            "type": "noul",
            "instructions": "Is Enemy.Hp gameplay state?",
            "criteria": {"true": "gameplay state", "false": "cosmetic"},
        }
    },
}


class FakeChain:
    def __init__(self, schema, raw) -> None:
        self.schema = schema
        self.raw = raw
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        return self.schema.model_validate(self.raw)


class FakeStructured:
    """`structured(model, schema, reasoning)` 를 대신한다. 받은 인자를 남긴다."""

    def __init__(self, raw) -> None:
        self.raw = raw
        self.calls: list[tuple] = []
        self.chain: FakeChain | None = None

    def __call__(self, model, schema, reasoning=None):
        self.calls.append((model, schema, reasoning))
        self.chain = FakeChain(schema, self.raw)
        return self.chain


def _decision_model(monkeypatch: pytest.MonkeyPatch, model: str | None) -> None:
    monkeypatch.setattr(
        decision,
        "get_model_settings_provider",
        lambda: SimpleNamespace(get_decision_model=lambda: model),
    )


def _fake_llm(monkeypatch: pytest.MonkeyPatch, raw) -> FakeStructured:
    fake = FakeStructured(raw)
    monkeypatch.setattr(chat_model, "structured", fake)
    return fake


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self.body = body
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.body


class FakeHttp:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def post(self, url: str, *, headers: dict, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def _keys(openrouter=None, typesafe=None):
    invalidated: list[bool] = []
    keys = SimpleNamespace(
        get_openrouter_key=lambda: openrouter,
        get_typesafe_key=lambda: typesafe,
        invalidate=lambda: invalidated.append(True),
        invalidated=invalidated,
    )
    return keys


# ── 누가 답하는가 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("configured", [None, "typesafe/jev-1.13"])
def test_null_or_the_jev_id_sends_the_question_to_jev(monkeypatch, configured) -> None:
    _decision_model(monkeypatch, configured)
    monkeypatch.setattr(decision, "get_llm_api_key_provider", lambda: _keys("or-key"))
    fake_llm = _fake_llm(monkeypatch, {})
    http = FakeHttp(FakeResponse({"answers": {"route": {"choice": "question"}}}))

    answer = asyncio.run(decision.decide(http, CHOICE_BODY))

    assert answer["answers"]["route"]["choice"] == "question"
    assert len(http.calls) == 1
    assert fake_llm.calls == []


def test_any_other_model_answers_through_the_chat_model(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    fake_llm = _fake_llm(monkeypatch, {"route": {"choice": "authoring"}})
    http = FakeHttp(FakeResponse({}))

    answer = asyncio.run(decision.decide(http, CHOICE_BODY))

    assert answer == {"answers": {"route": {"type": "choice", "choice": "authoring"}}}
    assert http.calls == []
    model, _schema, reasoning = fake_llm.calls[0]
    assert model == "openai/gpt-6-luna"
    assert reasoning is not None and reasoning.disabled


def test_resolve_decision_model_falls_back_to_jev(monkeypatch) -> None:
    _decision_model(monkeypatch, None)
    assert decision.resolve_decision_model() == "typesafe/jev-1.13"
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    assert decision.resolve_decision_model() == "openai/gpt-6-luna"


# ── 범용 LLM 의 답 검사 ─────────────────────────────────────────────────────


def test_the_choice_schema_is_an_enum_of_the_given_options(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    fake_llm = _fake_llm(monkeypatch, {"route": {"choice": "authoring"}})

    asyncio.run(decision.decide(None, CHOICE_BODY))

    schema = fake_llm.calls[0][1].model_json_schema()
    route = schema["properties"]["route"]
    referenced = schema["$defs"][route["$ref"].rsplit("/", 1)[-1]]
    assert referenced["properties"]["choice"]["enum"] == ["authoring", "question"]
    assert referenced["additionalProperties"] is False


def test_the_prompt_carries_state_instructions_and_criteria(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    fake_llm = _fake_llm(monkeypatch, {"route": {"choice": "authoring"}})

    asyncio.run(decision.decide(None, CHOICE_BODY))

    human = fake_llm.chain.messages[1].content
    assert "새 시나리오 만들어 줘" in human
    assert "Which branch?" in human
    assert "make scenarios" in human
    assert '"route": {"choice": <one of "authoring", "question">}' in human


def test_an_unknown_choice_is_a_decision_error(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"route": {"choice": "greeting"}})

    with pytest.raises(DecisionError):
        asyncio.run(decision.decide(None, CHOICE_BODY))


def test_an_unknown_choice_that_slips_past_the_schema_is_still_refused() -> None:
    request = decision.DecisionRequest.model_validate(CHOICE_BODY)

    with pytest.raises(DecisionError, match="greeting"):
        decision.to_jev_response(request, {"route": {"choice": "greeting"}})


def test_a_probability_in_range_comes_back_as_noul(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"m0": {"noul": 0.8}})

    answer = asyncio.run(decision.decide(None, NOUL_BODY))

    assert answer == {"answers": {"m0": {"type": "noul", "noul": 0.8}}}


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_a_probability_out_of_range_is_a_decision_error(monkeypatch, value) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"m0": {"noul": value}})

    with pytest.raises(DecisionError, match="0 과 1"):
        asyncio.run(decision.decide(None, NOUL_BODY))


def test_a_missing_answer_is_a_decision_error() -> None:
    request = decision.DecisionRequest.model_validate(NOUL_BODY)

    with pytest.raises(DecisionError, match="m0"):
        decision.to_jev_response(request, {})


def test_a_chat_model_failure_is_a_decision_error(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")

    def broken(model, schema, reasoning=None):
        raise RuntimeError("no key")

    monkeypatch.setattr(chat_model, "structured", broken)

    with pytest.raises(DecisionError, match="no key"):
        asyncio.run(decision.decide(None, CHOICE_BODY))


# ── 호출자는 실패 처리를 그대로 한다 ─────────────────────────────────────────


def test_the_macro_client_turns_an_llm_failure_into_jev_error(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"answer": {"choice": "jump"}})
    client = JevClient()
    client._http = SimpleNamespace()

    with pytest.raises(JevError):
        asyncio.run(client.choose("Pick one.", {"attack": "hit", "heal": "rest"}, {"hp": 3}))


def test_the_macro_client_takes_an_llm_answer(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"answer": {"noul": 0.25}})
    client = JevClient()
    client._http = SimpleNamespace()

    probability = asyncio.run(client.probability("Is it over?", {"hp": 0}, "over", "not over"))

    assert probability == 0.25


def test_reach_answers_from_an_llm(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"verdict": {"noul": 0.2}})
    judge = reach.UnreachedJudge()
    judge._http = SimpleNamespace()

    unreached = asyncio.run(judge.unreached_probability("open the shop", "shop opens", "not yet"))

    assert unreached == pytest.approx(0.8)


def test_reach_fails_open_when_the_llm_answers_out_of_range(monkeypatch) -> None:
    _decision_model(monkeypatch, "openai/gpt-6-luna")
    _fake_llm(monkeypatch, {"verdict": {"noul": 3.0}})
    judge = reach.UnreachedJudge()
    judge._http = SimpleNamespace()

    assert asyncio.run(judge.unreached_probability("a", "b", "c")) is None


# ── Jev: TypeSafe 직접 호출과 OpenRouter ───────────────────────────────────────

TYPESAFE_CHOICE_RESPONSE = {
    "model": "jev-1.13.0",
    "answers": {
        "answer": {
            "type": "choice",
            "choice": "heal",
            "probabilities": {"attack": 0.1, "heal": 0.9},
            "confidence": 0.8,
        }
    },
    "usage": {"input_tokens": 120, "output_tokens": 3},
}


def test_the_typesafe_key_wins_over_the_openrouter_key(monkeypatch) -> None:
    _decision_model(monkeypatch, None)
    monkeypatch.setattr(
        decision, "get_llm_api_key_provider", lambda: _keys("or-key", "ts-key")
    )
    http = FakeHttp(FakeResponse(TYPESAFE_CHOICE_RESPONSE))

    asyncio.run(decision.decide(http, CHOICE_BODY))

    call = http.calls[0]
    assert call["url"] == "https://api.typesafe.ai/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer ts-key"
    assert "HTTP-Referer" not in call["headers"]
    assert call["json"]["model"] == "jev-1.13.0"
    assert call["json"]["questions"] == CHOICE_BODY["questions"]


def test_the_typesafe_base_url_is_a_setting(monkeypatch) -> None:
    _decision_model(monkeypatch, None)
    monkeypatch.setattr(decision, "get_llm_api_key_provider", lambda: _keys(None, "ts-key"))
    settings = decision.get_settings().model_copy(
        update={"typesafe_base_url": "http://typesafe.test/"}
    )
    monkeypatch.setattr(decision, "get_settings", lambda: settings)
    http = FakeHttp(FakeResponse(TYPESAFE_CHOICE_RESPONSE))

    asyncio.run(decision.decide(http, CHOICE_BODY))

    assert http.calls[0]["url"] == "http://typesafe.test/v1/systemone"


def test_without_a_typesafe_key_jev_goes_through_openrouter(monkeypatch) -> None:
    _decision_model(monkeypatch, None)
    monkeypatch.setattr(decision, "get_llm_api_key_provider", lambda: _keys("or-key", None))
    http = FakeHttp(FakeResponse({"answers": {}}))

    asyncio.run(decision.decide(http, CHOICE_BODY))

    call = http.calls[0]
    assert call["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert call["headers"]["Authorization"] == "Bearer or-key"
    assert call["json"]["model"] == "typesafe/jev-1.13"


@pytest.mark.parametrize(("openrouter", "typesafe"), [("or-key", None), (None, "ts-key")])
def test_a_401_drops_the_key_cache(monkeypatch, openrouter, typesafe) -> None:
    _decision_model(monkeypatch, None)
    keys = _keys(openrouter, typesafe)
    monkeypatch.setattr(decision, "get_llm_api_key_provider", lambda: keys)
    http = FakeHttp(FakeResponse({}, status_code=401))

    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(decision.decide(http, CHOICE_BODY))

    assert keys.invalidated == [True]


def test_a_typesafe_choice_response_with_probabilities_is_read(monkeypatch) -> None:
    async def post(body: dict) -> dict:
        return TYPESAFE_CHOICE_RESPONSE

    chosen = asyncio.run(
        JevClient(post).choose("Pick.", {"attack": "hit", "heal": "rest"}, {"hp": 3})
    )

    assert chosen == "heal"


def test_a_typesafe_noul_response_without_cost_is_read() -> None:
    async def post(body: dict) -> dict:
        return {
            "model": "jev-1.13.0",
            "answers": {"answer": {"type": "noul", "noul": 0.3}},
            "usage": {"input_tokens": 90, "output_tokens": 1},
        }

    probability = asyncio.run(JevClient(post).probability("Over?", {}, "over", "not over"))

    assert probability == 0.3


def test_relevance_reads_a_typesafe_noul_response() -> None:
    from app.qa.pulse import PulseMemory

    async def post(body: dict) -> dict:
        return {
            "model": "jev-1.13.0",
            "answers": {name: {"type": "noul", "noul": 0.7} for name in body["questions"]},
            "usage": {"input_tokens": 90, "output_tokens": 1},
        }

    judge = relevance.PulseRelevanceJudge(decide=post)
    memory = PulseMemory()
    sample = relevance.MemberSample(
        type_key="Enemy::Hp", object="Enemy", component="Enemy", member="Hp", value="3"
    )

    cost = asyncio.run(judge._judge_chunk(memory, [sample]))

    assert memory.relevance["Enemy::Hp"] == 0.7
    assert cost == 0.0
