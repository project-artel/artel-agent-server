"""Bedrock 이 키를 어디서 받는가.

이 배선은 조용히 깨지는 종류다. 키가 안 닿아도 예외는 안 나고, 첫 호출이 인증 실패로
돌아올 뿐이다 — 그 시점에는 이미 런이 시작해 있고, 원인은 키 값이 틀린 것처럼 보인다.
"""

import os

import pytest

from app.llm.chat_model import build_chat_model

BEDROCK_MODEL = "bedrock/us.anthropic.claude-haiku-5-5"
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


class _FixedKeys:
    """Stands in for `LlmApiKeyProvider`: holds a Bedrock key a test can change."""

    def __init__(self, bedrock: str | None) -> None:
        self.bedrock = bedrock

    def get_bedrock_key(self) -> str | None:
        return self.bedrock

    def invalidate(self) -> None:
        return None


def _use_keys(monkeypatch, keys: _FixedKeys) -> None:
    monkeypatch.setattr("app.llm.chat_model.get_llm_api_key_provider", lambda: keys)


def test_the_stored_key_travels_to_the_client(monkeypatch) -> None:
    """관리자가 admin page 에 넣은 Bedrock key 가 `ChatBedrockConverse` 까지 가야 한다."""
    _use_keys(monkeypatch, _FixedKeys("test-key"))

    model = build_chat_model(BEDROCK_MODEL)

    assert model.bedrock_api_key is not None
    assert model.bedrock_api_key.get_secret_value() == "test-key"


def test_a_changed_key_reaches_a_new_client_without_a_restart(monkeypatch) -> None:
    """`build_chat_model` 은 client 를 cache 하지만, Bedrock key 가 바뀌면 새 client 를 준다."""
    keys = _FixedKeys("old-key")
    _use_keys(monkeypatch, keys)

    first = build_chat_model(BEDROCK_MODEL)
    assert build_chat_model(BEDROCK_MODEL) is first

    keys.bedrock = "new-key"
    second = build_chat_model(BEDROCK_MODEL)

    assert second is not first
    assert second.bedrock_api_key.get_secret_value() == "new-key"


def test_no_key_leaves_the_standard_aws_chain_alone(monkeypatch) -> None:
    """키가 저장되지 않은 배포는 표준 AWS 자격증명 사슬(IAM role 등)로 간다.

    예전 설정인 `BEDROCK_API_KEY` 는 세워 둔 채로 검사한다. 그 값이 client 에 실리면
    키가 database 가 아닌 곳에서 온 것이다.

    `AWS_BEARER_TOKEN_BEDROCK` 은 지운다. 그것은 AWS 자신의 표준 자격증명이라 사슬의
    일부이고, langchain-aws 1.8 은 `bedrock_api_key=None` 을 받아도 그 변수를 읽는다.
    셸에 남은 값에 이 테스트가 흔들리지 않게 하려고 지운다.
    """
    monkeypatch.delenv(BEARER_ENV, raising=False)
    monkeypatch.setenv("BEDROCK_API_KEY", "env-key")
    _use_keys(monkeypatch, _FixedKeys(None))

    model = build_chat_model(BEDROCK_MODEL)

    assert model.bedrock_api_key is None
