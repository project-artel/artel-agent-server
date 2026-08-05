"""저작 Step이 실린 실행 케이스 모델 — Orche 계약(scenario.cases[], ARTEL-254/258).

Orche는 QA 세션 오픈 시 각 시나리오의 `scenario.cases[]`로 조합(TC 내용 + position별
저작 Step)을 보낸다. Agent는 이 cases를 **가공해 실행 스텝**을 만든다:

- `setup`(assert=false): 사전조건 도달 경로. fast-forward — 판정하지 않고 상태에만 도달.
- `guide`: TC 실행 단계.
- `verify`: 검증(있으면).

Step은 **advisory**다: 실행 시 씬이 step과 다르면 Agent가 무시하고 자기 판단으로 진행한다.
`hint`(키/조작)는 있으면 근거로 쓰되 강제가 아니다(키바인딩 자동 소스가 없어 MVP는 intent 위주).
"""

from pydantic import BaseModel, ConfigDict, Field


class QaStep(BaseModel):
    """cases[].steps[] 한 칸.

    `assert`는 파이썬 키워드라 alias로 받는다(`populate_by_name`으로 필드명 `asserts`도 허용).
    `kind`는 forward-compat 위해 str로 둔다 — Orche가 새 kind를 보내도 파싱을 깨지 않는다(모르면
    기본 취급). `extra="allow"`로 미지 필드도 버리지 않는다.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str = ""
    kind: str = "guide"  # setup | guide | verify
    asserts: bool = Field(default=True, alias="assert")
    intent: str = ""
    hint: str | None = None
    input: str | None = None  # "keyboard" | "click" (interactable 유무로 추론된 값, 없으면 None)
    observe: str | None = None  # verify가 볼 대상


class QaCase(BaseModel):
    """cases[] 한 칸 = 실행할 TC(내용) + 그 자리의 저작 Step."""

    model_config = ConfigDict(extra="allow")

    position: int = 0
    title: str = ""
    category: str = ""
    precondition: str | None = None
    expected: str = ""
    steps: list[QaStep] = Field(default_factory=list)
