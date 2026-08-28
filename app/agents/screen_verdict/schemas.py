"""제안 하나에 대한 답, 그리고 그 답이 통과해야 하는 문.

모델이 내놓는 모양(`ProposedEntry` · `ProposedVerdict`)과 프레임에 실어도 되는
모양(`app.qa.envelope.ScreenSelectorEntry`)을 나눠 둔다. 둘이 같아 보이지만 사이에 검증이
있고, 그 검증이 이 파일이 존재하는 이유다 — **모델이 지어낸 항목은 저장되면 그 `scene` 을
영구히 잘못 가른다.** 잘못 넣은 항목은 다음 관측부터 화면을 갈라 놓고, 그것을 되돌리는
항목은 이미 갈라진 과거를 복원하지 못한다.

`match` 셋과 `pattern` 상한은 계약이 정한 값이라 여기서 새로 정의하지 않고
`app/agents/qa/screen.py` 의 것을 그대로 쓴다. 같은 계약 값이 이 저장소에 두 벌 있으면
언젠가 한 벌만 고쳐지고, 그때 어느 쪽이 맞는지 아무도 모른다.
"""

from pydantic import BaseModel, Field

from app.llm.models import DEFAULT_MODEL, LLMModel
from app.qa.envelope import ScreenSelectorProposalPayload

class ProposedEntry(BaseModel):
    """모델이 내놓은 항목 하나. **아직 아무것도 보장되지 않는다.**

    필드가 전부 필수인 것은 구조화 출력이 그렇게 요구해서이고, 값이 쓸 만한지는 별개다 —
    셋 중 하나가 아닌 `match`, 후보에 없던 `pattern`, 빈 `reason` 이 전부 여기까지는
    통과한다. 거르는 것은 `validate.py` 다.
    """

    match: str
    pattern: str
    screen_defining: bool
    reason: str


class ProposedVerdict(BaseModel):
    """모델의 구조화 출력 그대로.

    `entries` 가 비어도 정상이다. 기본값이 무시라 "물어본 것 중 화면을 가르는 것이 없다"
    가 완전한 답이고, 그 경우 저장할 것이 없다.
    """

    entries: list[ProposedEntry] = Field(default_factory=list)
    note: str | None = None


class ScreenVerdictRequest(BaseModel):
    """판정 한 건에 필요한 전부.

    `proposal` 만 보고 답한다. 이 서버가 알고 있는 다른 것 — QA 런의 시나리오, 그 런이
    지금까지 본 것, 이 프로젝트의 게임 — 을 섞지 않는다. 섞는 순간 그 게임에서만 맞는
    판정기가 되고, 이 이슈가 존재하는 이유가 정확히 그것을 피하는 것이다.
    """

    proposal: ScreenSelectorProposalPayload
    model: LLMModel = DEFAULT_MODEL
