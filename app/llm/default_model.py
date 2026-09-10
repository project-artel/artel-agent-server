"""model 을 안 지정한 호출이 어느 model 로 가나.

`models.py` 와 나눠 둔 이유는 그 파일이 바깥 import 가 하나도 없는 catalog 라서다.
어느 model 이 있고 각각 `context window` 가 얼마인지는 값이고, 그중 무엇을 기본으로
쓰는지는 이 배포의 설정이다. 후자를 catalog 에 넣으면 catalog 가 `Settings` 를
알아야 한다.
"""

from app.config import get_settings
from app.llm.models import LLMModel


# `DEFAULT_MODEL` 을 안 준 배포가 쓰는 model. 설정 항목이 생기기 전까지 이 값이
# 유일한 기본이었고, 값을 그대로 둔 것은 `DEFAULT_MODEL` 을 설정하지 않은 배포의
# 동작을 바꾸지 않기 위해서다.
#
# 이름이 `DEFAULT_MODEL` 이 아닌 이유: 환경변수와 `Settings.default_model` 이 그
# 이름을 쓴다. 셋이 같은 이름이면 "상수는 기본이 아니라 못 정했을 때의 값" 이라는
# 사실을 읽는 사람이 외워야 한다.
FALLBACK_MODEL: LLMModel = LLMModel.gpt_5_6_luna


def resolve_default_model() -> LLMModel:
    """이 배포의 기본 model. 요청이 `model` 을 안 실어 보낼 때 쓰이는 값이다.

    호출 시점에 설정을 읽는 것이 이 함수의 전부이고, 그 자리가 중요하다. Pydantic
    은 class 가 정의될 때 field default 를 한 번 읽으므로, 요청 schema 가
    `model: LLMModel = FALLBACK_MODEL` 로 적혀 있으면 `.env` 를 바꿔도 안 따라간다.
    `Field(default_factory=resolve_default_model)` 로 적어야 인스턴스를 만들 때마다
    설정을 본다. 함수 인자 쪽도 같은 이유로 `LLMModel | None = None` 을 받고 안에서
    이 함수를 부른다.

    catalog 밖 값은 여기까지 오지 않는다. `Settings.known_model` 이 첫
    `get_settings()` 에서 거절하므로, 오타는 첫 호출이 아니라 startup 에서 죽는다.
    """
    configured = get_settings().default_model
    return LLMModel(configured) if configured else FALLBACK_MODEL
