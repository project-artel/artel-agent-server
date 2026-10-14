"""런을 시작한 사용자가 등록한 LLM API key 를 그 런의 model 호출에 묶는다.

공용 key 는 `app/llm/api_key.py` 가 orchestration 에서 읽는다. 사용자 key 는 그렇게 끌어오지
않는다 — orchestration 이 `POST /internal/qa-sessions` 의 `llm_key` 로 실어 보내고, 이 서버는
그것을 세션 기록에만 둔다. key 의 수명이 세션과 같으므로 사용자별 cache 도 무효화도 없다.

chat model 은 `lru_cache` 로 프로세스 전체가 나눠 쓰므로 key 를 인자로 실어 나를 수 없다.
그래서 usage scope(`app/llm/usage.py`)처럼 `ContextVar` 에 둔다. 세션 하나를 맡은 task 에서
묶으면 그 task 가 띄우는 런과, receive loop 가 런 옆에서 띄우는 `screen_verdict` ·
`screen_name` 호출이 모두 같은 key 를 읽는다.

key 는 로그·`repr`·응답에 남기지 않는다. `LlmKey.api_key` 는 `repr=False` 다.
"""

from contextvars import ContextVar
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class LlmKeyService(StrEnum):
    """사용자 key 를 받는 서비스. 값은 orchestration 의 `user_llm_key.service` 와 같다."""

    anthropic = "anthropic"
    openai = "openai"
    openrouter = "openrouter"


class LlmKey(BaseModel):
    """사용자 key 하나. 요청 본문, 세션 기록, 묶인 값이 모두 이 모양이다.

    `api_key` 는 `SecretStr` 이 아니다. 세션 기록이 `model_dump_json` 으로 Redis 에 저장되는데,
    `SecretStr` 은 거기서 `**********` 로 바뀌어 다시 읽을 수 없다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    service: LlmKeyService
    api_key: str = Field(min_length=1, repr=False)


_bound_key: ContextVar[LlmKey | None] = ContextVar("llm_key", default=None)


def bind_llm_key(key: LlmKey | None) -> None:
    """이 task 와 이후 띄우는 자식 task 의 model 호출에 `key` 를 쓴다. None 이면 공용 key 다."""
    _bound_key.set(key)


def bound_llm_key() -> LlmKey | None:
    """지금 task 에 묶인 사용자 key. 없으면 None 이다."""
    return _bound_key.get()
