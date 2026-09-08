"""Bedrock 이 키를 어디서 받는가.

이 배선은 조용히 깨지는 종류다. 키가 안 닿아도 예외는 안 나고, 첫 호출이 인증 실패로
돌아올 뿐이다 — 그 시점에는 이미 런이 시작해 있고, 원인은 키 값이 틀린 것처럼 보인다.
"""

import os

import pytest

from app.config import Settings
from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel

BEDROCK_MODEL = LLMModel.claude_haiku_4_5_bedrock
BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"


@pytest.fixture(autouse=True)
def _no_bearer_token_leaks():
    """이 파일의 테스트마다 `AWS_BEARER_TOKEN_BEDROCK` 을 원래대로 돌린다.

    `ChatBedrockConverse` 는 `bedrock_api_key` 를 받으면 그 값을 **프로세스 전역
    환경변수로 설정한다**(제 docstring 이 경고한다). 그래서 키를 주는 테스트 하나가
    같은 프로세스의 뒤 테스트에 그 키를 남기고, 실제로 "키 없이 지었는데 키가 있다" 로
    깨졌다. 이 서버는 한 배포가 한 자격증명을 쓰므로 그 전역 설정 자체는 문제가 아니지만,
    테스트는 서로를 오염시키면 안 된다.
    """
    before = os.environ.get(BEARER_ENV)
    # `build_chat_model` 은 `lru_cache` 다. 비우지 않으면 두 테스트가 같은 객체를 받고,
    # 둘째 테스트는 제가 지은 적 없는 모델을 검사하게 된다.
    build_chat_model.cache_clear()
    yield
    build_chat_model.cache_clear()
    if before is None:
        os.environ.pop(BEARER_ENV, None)
    else:
        os.environ[BEARER_ENV] = before


def test_the_key_travels_from_settings_to_the_client(monkeypatch) -> None:
    """`.env` 의 `BEDROCK_API_KEY` 가 `ChatBedrockConverse` 까지 가야 한다.

    `boto3` 가 읽는 `AWS_BEARER_TOKEN_BEDROCK` 을 `.env` 로 줄 수 없어서 생긴 경로다.
    `SettingsConfigDict(env_file=".env")` 는 그 파일을 `Settings` 로만 읽고
    `os.environ` 에는 넣지 않는데 그 변수를 보는 것은 `boto3` 다 — `.env` 에 그 이름으로
    적으면 아무 말 없이 무시된다.
    """
    settings = Settings(_env_file=None, bedrock_api_key="test-key")
    monkeypatch.setattr("app.llm.chat_model.get_settings", lambda: settings)

    model = build_chat_model(BEDROCK_MODEL)

    assert model.bedrock_api_key is not None
    assert model.bedrock_api_key.get_secret_value() == "test-key"


def test_no_key_leaves_the_standard_aws_chain_alone(monkeypatch) -> None:
    """키를 안 쥔 배포는 종전과 똑같이 돌아야 한다.

    `None` 을 넘기면 `ChatBedrockConverse` 가 제 기본값(`AWS_BEARER_TOKEN_BEDROCK`
    환경변수, 그다음 표준 AWS 자격증명 사슬)으로 간다. 빈 문자열을 넘기는 것과 다르다 —
    그쪽은 키를 쥐었다고 주장하면서 아무것도 인증하지 못한다.

    `_env_file=None` 으로 짓는다. 그러지 않으면 이 기계의 `.env` 가 답을 정한다 —
    `InitSettingsSource` 가 `None` 인 인자를 "안 준 것" 으로 보고 넘겨서, 키를 쥔
    개발 기계에서만 이 테스트가 깨진다.
    """
    settings = Settings(_env_file=None)
    monkeypatch.delenv(BEARER_ENV, raising=False)
    monkeypatch.setattr("app.llm.chat_model.get_settings", lambda: settings)

    model = build_chat_model(BEDROCK_MODEL)

    assert model.bedrock_api_key is None
