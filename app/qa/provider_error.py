"""다시 해도 안 되는 모델 API 오류를 종류로 가른다(ARTEL-1370).

크레딧 부족 · 인증 · 없는 모델은 같은 키와 같은 모델로 다시 불러도 같은 답이 온다. 이런 오류가 한
시나리오에서 나면 남은 시나리오도 모두 같은 이유로 실패하므로, run 루프는 그 자리에서 런을 멈추고
여기서 가른 코드를 orchestration 에 보낸다. 그 밖의 오류(시간 초과, 잠깐의 과부하, 우리 코드의 예외)는
[classify_provider_error] 가 `None` 을 돌려주고 지금처럼 그 시나리오만 실패로 닫는다.

제공사 SDK 를 import 하지 않고 예외의 모양(`status_code` · `body` · `code` · botocore `response`)만 본다.
SDK 가 바뀌어도 이 파일이 깨지지 않게 하려는 것이고, 테스트에서 가짜 예외로 흉내 내기도 쉽다.
"""

from __future__ import annotations

from dataclasses import dataclass

# orchestration 과의 계약이다. 이름을 바꾸면 orchestration 의 `QaStopCode` 도 함께 바꾼다.
CREDIT_EXHAUSTED = "llm_credit_exhausted"
AUTH_FAILED = "llm_auth_failed"
MODEL_UNAVAILABLE = "llm_model_unavailable"

# 원문은 화면 아래 작은 줄과 로그에만 쓴다. 제공사가 긴 본문을 보내도 이만큼만 싣는다.
MAX_PROVIDER_MESSAGE = 500

# 사람이 읽는 한 줄. 제공사 이름을 넣지 않는다 — 화면과 로그가 회사 이름이 아니라 무엇이 모자란지를 말하게 한다.
# 어느 제공사인지는 `provider` 칸에 따로 있다.
_SUMMARIES = {
    CREDIT_EXHAUSTED: "The model API token balance is exhausted, so the run stopped.",
    AUTH_FAILED: "The model API rejected the API key, so the run stopped.",
    MODEL_UNAVAILABLE: "The model API does not serve the requested model, so the run stopped.",
}

# 키가 없어서 모델을 만들지도 못한 경우. 제공사에 묻기 전에 `app.llm.chat_model` 이 던진다.
_MISSING_KEY_ERRORS = {
    "ClaudeApiKeyMissingError": "anthropic",
    "GptApiKeyMissingError": "openai",
}

_BEDROCK_AUTH_CODES = {
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "ExpiredTokenException",
    "AccessDeniedException",
}


@dataclass(frozen=True)
class ProviderFailure:
    """런을 멈출 제공사 오류 하나."""

    code: str
    provider: str
    provider_message: str

    @property
    def summary(self) -> str:
        """사람이 읽는 한 줄. 화면은 `code` 로 제 말을 고르고, 이 줄은 로그와 다른 소비자용이다."""
        return _SUMMARIES[self.code]

    def as_detail(self) -> dict:
        return {"provider": self.provider, "provider_message": self.provider_message}

    def as_stop(self) -> dict:
        """끝남 STATUS 의 `stop`. orchestration 이 시도 · 런에 그대로 저장한다."""
        return {"code": self.code, "provider": self.provider, "message": self.summary}


def classify_provider_error(error: BaseException) -> ProviderFailure | None:
    """[error] 와 그 원인 사슬에서 런을 멈출 제공사 오류를 찾는다. 없으면 `None`."""
    for candidate in _causes(error):
        found = _classify_one(candidate)
        if found is not None:
            return found
    return None


def _causes(error: BaseException):
    """langchain · 재시도 래퍼가 원래 예외를 `__cause__` · `__context__` 에 감싸 두는 경우까지 본다."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _classify_one(error: BaseException) -> ProviderFailure | None:
    name = type(error).__name__
    if name in _MISSING_KEY_ERRORS:
        return ProviderFailure(AUTH_FAILED, _MISSING_KEY_ERRORS[name], _clip(str(error)))
    response = getattr(error, "response", None)
    if isinstance(response, dict) and "Error" in response:
        return _classify_bedrock(error, response)
    status = getattr(error, "status_code", None)
    if not isinstance(status, int):
        return None
    provider = _provider_of(error)
    if provider is None:
        return None
    message = _provider_text(error)
    lowered = message.lower()
    code = getattr(error, "code", None)
    if status == 402 or code == "insufficient_quota" or "credit balance" in lowered or "insufficient credits" in lowered:
        return ProviderFailure(CREDIT_EXHAUSTED, provider, _clip(message))
    if status in (401, 403):
        return ProviderFailure(AUTH_FAILED, provider, _clip(message))
    if status == 404 or (status == 400 and _names_missing_model(lowered)):
        return ProviderFailure(MODEL_UNAVAILABLE, provider, _clip(message))
    return None


def _classify_bedrock(error: BaseException, response: dict) -> ProviderFailure | None:
    detail = response.get("Error") or {}
    code = str(detail.get("Code", ""))
    message = str(detail.get("Message", "")) or str(error)
    if code in _BEDROCK_AUTH_CODES:
        return ProviderFailure(AUTH_FAILED, "bedrock", _clip(message))
    if code == "ResourceNotFoundException" or (code == "ValidationException" and _names_missing_model(message.lower())):
        return ProviderFailure(MODEL_UNAVAILABLE, "bedrock", _clip(message))
    return None


def _provider_of(error: BaseException) -> str | None:
    module = type(error).__module__ or ""
    if module.startswith("anthropic"):
        return "anthropic"
    if module.startswith("openai"):
        # OpenRouter 는 OpenAI 호환 클라이언트로 부른다. 어느 쪽인지는 요청 주소로만 안다.
        host = ""
        request = getattr(error, "request", None)
        if request is not None:
            host = str(getattr(getattr(request, "url", None), "host", "") or "")
        return "openrouter" if "openrouter" in host else "openai"
    return None


def _provider_text(error: BaseException) -> str:
    """제공사가 보낸 사람 말. 본문에 `error.message` 가 있으면 그것을, 없으면 예외 문자열을 쓴다."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        text = inner.get("message")
        if isinstance(text, str) and text:
            return text
    return str(error)


def _names_missing_model(lowered: str) -> bool:
    return "model" in lowered and any(
        phrase in lowered for phrase in ("not found", "does not exist", "invalid model", "identifier is invalid", "not supported")
    )


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_PROVIDER_MESSAGE else text[: MAX_PROVIDER_MESSAGE - 1] + "…"
