"""요청이 model을 생략하면 배포 설정을 현재 DB catalog와 대조해 선택한다."""

from app.config import get_settings
from app.llm.models import DEFAULT_MODEL, LLMModel, require_enabled_model


FALLBACK_MODEL: str = DEFAULT_MODEL


def resolve_default_model() -> LLMModel:
    """catalog는 원격에서 읽으므로 Settings 초기화 대신 실제 선택 시 검증한다."""
    configured = get_settings().default_model
    if not configured:
        return FALLBACK_MODEL
    try:
        return require_enabled_model(configured)
    except ValueError as error:
        raise ValueError(f"default_model (DEFAULT_MODEL): {error}") from error
