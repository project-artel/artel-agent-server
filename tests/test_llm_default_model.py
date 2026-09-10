"""어느 model 이 model 을 안 지정한 요청에 답하나.

이 파일이 지키는 것은 하나다: 그 답이 코드가 아니라 배포 설정에서 나온다.
"""

import asyncio

from app.agents.game_context.schemas import GameContextAgentRequest
from app.agents.knowledge_query.schemas import KnowledgeQueryAgentRequest
from app.agents.scenario.schemas import ScenarioAgentRequest
from app.agents.screen_verdict.schemas import ScreenVerdictRequest
from app.agents.step_phrasing.schemas import StepPhrasingRequest
from app.api.extract import ExtractRequest
from app.api.knowledge_queries import KnowledgeQueriesRequest
from app.api.qa_sessions import OpenQaSessionRequest
from app.api.sessions import OpenSessionRequest
from app.api.step_phrasing import StepPhrasingBody
from app.config import Settings
from app.llm.default_model import FALLBACK_MODEL, resolve_default_model
from app.llm.models import LLMModel
from app.qa.run_config import resolve_run_config
from app.sessions.schemas import SessionRecord
from app.sessions.store import InMemorySessionStore


# 지금 catalog 에서 Bedrock 으로 가는 유일한 항목. `bedrock/` 접두가
# `build_chat_model` 을 다른 provider 로 보내므로, 이 값으로 바뀌는지를 보면
# "설정이 provider 를 옮긴다" 까지 확인된다.
BEDROCK = LLMModel.claude_haiku_4_5_bedrock
# 기본값과 겹치지 않는 아무 항목. 요청이 지정한 model 이 살아남는지 볼 때 쓴다.
NAMED = LLMModel.gpt_chat_latest


def _use(monkeypatch, default_model: str | None) -> Settings:
    """`resolve_default_model` 이 읽는 자리만 바꾼다.

    `get_settings` 는 `lru_cache` 라 비우면 같은 프로세스의 뒤 테스트를 오염시킨다.
    그리고 `resolve_default_model` 자체를 patch 하면 안 먹는다 — `default_factory`
    가 함수 객체를 만들 때 잡아 두기 때문이다. 그래서 그 module 이 보는
    `get_settings` 를 바꾼다.
    """
    settings = Settings(_env_file=None, default_model=default_model)
    monkeypatch.setattr("app.llm.default_model.get_settings", lambda: settings)
    return settings


def test_an_unset_default_keeps_the_fallback(monkeypatch) -> None:
    """설정하지 않은 배포는 종전과 똑같이 돌아야 한다. 그것이 이 변경의 rollback
    경로이기도 하다 — `.env` 에서 한 줄 지우면 예전 동작이다."""
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    settings = _use(monkeypatch, None)

    assert settings.default_model is None
    assert resolve_default_model() is FALLBACK_MODEL


def test_the_setting_moves_the_default(monkeypatch) -> None:
    _use(monkeypatch, BEDROCK.value)

    assert resolve_default_model() is BEDROCK


def test_a_request_naming_no_model_takes_the_deployment_default(monkeypatch) -> None:
    """이 단정이 이 변경의 전부다.

    Pydantic 은 class 가 정의될 때 field default 를 한 번 읽는다. 그래서 요청
    schema 에 상수를 그대로 적어 두면 `.env` 를 바꿔도 안 따라가고, 그때 깨지는
    것이 이 테스트다. `POST /extract` 를 고른 것은 이 경로가 실제로 실패한
    경로이기 때문이다 — orchestration 이 `model` 을 안 실어 보낸다.
    """
    _use(monkeypatch, BEDROCK.value)

    request = ExtractRequest(source_url="https://example.test/doc.pdf", filename="doc.pdf")

    assert request.model is BEDROCK


def test_a_request_naming_a_model_is_left_alone(monkeypatch) -> None:
    """설정은 기본값만 정한다. 지정해 보내는 호출자는 영향이 없어야 한다."""
    _use(monkeypatch, BEDROCK.value)

    request = ExtractRequest(
        source_url="https://example.test/doc.pdf", filename="doc.pdf", model=NAMED
    )

    assert request.model is NAMED


def test_a_run_opened_without_a_model_records_the_deployment_default(monkeypatch) -> None:
    """함수 인자 쪽도 설정을 봐야 한다.

    `resolve_run_config` 의 `model` 은 인자 기본값이라 schema field 와 다른 방식으로
    고쳤다. 한쪽만 설정을 보면 두 기본값이 서로 다른 답을 내고, 그것이 ARTEL-776
    에서 이미 한 번 일어난 실패다. `provider` 까지 보는 것은 그 값이 model 에서
    파생되므로, 기본이 정말 옮겨졌다면 함께 움직여야 하기 때문이다.
    """
    _use(monkeypatch, BEDROCK.value)

    config = resolve_run_config()

    assert config.model is BEDROCK
    assert config.provider == "anthropic"


def test_a_stored_session_keeps_the_model_it_was_opened_with(monkeypatch) -> None:
    """저장된 session 은 지금 설정을 따라가면 안 된다.

    `SessionRecord.model` 도 `default_factory` 라, 직렬화가 그 field 를 빼면 되읽을
    때 `resolve_default_model()` 이 돌아 그 session 이 열린 적 없는 model 로 이어진다.
    지금은 `InMemorySessionStore.save` 가 `model_dump_json()` 을 옵션 없이 부르므로
    안전하다.

    저장하는 값이 `FALLBACK_MODEL` 인 것이 이 테스트의 전부다. 그 자리에 기본값과
    다른 model 을 쓰면 `exclude_defaults=True` 가 붙어도 field 가 실려 나가 단정이
    통과해 버린다 — 지키려던 것을 안 지키면서 초록불을 준다. 기본값과 같은 값이라야
    그 옵션이 field 를 지우고, 되읽을 때 그때의 설정을 따라가는 것이 드러난다.
    """
    store = InMemorySessionStore()
    asyncio.run(store.save("session", SessionRecord(model=FALLBACK_MODEL)))

    _use(monkeypatch, BEDROCK.value)

    assert asyncio.run(store.load("session")).model is FALLBACK_MODEL


def test_every_request_schema_reads_the_setting(monkeypatch) -> None:
    """기본값을 쓰는 Pydantic field 11 곳 전부가 설정을 봐야 한다.

    위 테스트들은 `ExtractRequest` 하나만 실제로 지나간다. 나머지 열 곳 중 아무거나
    상수 default 로 되돌려도 그것들은 초록이다 — `SessionRecord`,
    `OpenQaSessionRequest`, `ScenarioAgentRequest` 는 실행 경로에 있는데도 그렇다.

    wiring 만 보는 약한 단정이지만, 없으면 그 wiring 을 보는 것이 하나도 없다. 새
    요청 schema 에 `model` field 를 더하는 사람은 이 목록에도 더해야 한다.
    """
    schemas = (
        ExtractRequest,
        OpenQaSessionRequest,
        OpenSessionRequest,
        KnowledgeQueriesRequest,
        StepPhrasingBody,
        SessionRecord,
        GameContextAgentRequest,
        KnowledgeQueryAgentRequest,
        ScreenVerdictRequest,
        ScenarioAgentRequest,
        StepPhrasingRequest,
    )

    assert len(schemas) == 11
    for schema in schemas:
        field = schema.model_fields["model"]
        assert field.default_factory is resolve_default_model, schema.__name__
