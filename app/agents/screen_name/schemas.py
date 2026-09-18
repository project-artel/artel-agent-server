"""`screen` 하나에 이름을 짓는 데 필요한 것, 그리고 모델이 내놓는 모양.

모델이 내놓는 모양(`ProposedName`)과 frame 에 실어도 되는 모양
(`app.qa.envelope.ScreenNamePayload`)을 나눠 둔다. 사이에 `validate.py` 가 있고, 그것이
보는 것은 길이와 빈 문자열과 selector 복사뿐이다 — 이름이 좋은지는 기계가 답할 수 있는
질문이 아니다.

`screen` 과 `scene` 을 계약 모델 그대로 받는다. 이 저장소에 같은 값의 두 번째 모델을 두면
한쪽만 필드가 늘고 그것이 조용하다.
"""

from pydantic import BaseModel

from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.envelope import ScreenSelectorSceneRef, ScreenSelectorScreenRef


class ProposedName(BaseModel):
    """모델이 내놓은 답 그대로. **아직 아무것도 보장되지 않는다.**

    `name` 이 `None` 인 답이 정상이다. 근거가 이름 하나를 받치지 못하면 짓지 않는 것이
    완전한 답이고, 그 경우 `screen` 은 이름 없이 남는다.
    """

    name: str | None = None
    note: str | None = None


class ScreenNameRequest(BaseModel):
    """이름 한 건에 필요한 전부.

    `screen` 과 `scene` 만 보고 짓는다. 이 서버가 알고 있는 다른 것 — QA 런의 시나리오,
    그 런이 지금까지 본 것 — 을 섞지 않는다. `screen_verdict` 와 같은 판단이고, 이유도
    같다: 섞는 순간 그 런의 문맥에서만 맞는 이름이 나오고, 이름은 그 런이 끝난 뒤에도
    content map 에 남는다.
    """

    screen: ScreenSelectorScreenRef
    scene: ScreenSelectorSceneRef
    model: LLMModel = DEFAULT_MODEL
