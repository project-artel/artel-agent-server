"""모델이 내놓은 이름 중 frame 에 실어도 되는 것만 남긴다.

## 왜 `screen_verdict/validate.py` 만큼 하지 않는가

저쪽이 그렇게 긴 이유는 잘못 앉은 항목 하나가 그 `scene` 의 화면을 다음 관측부터 갈라
놓고 되돌릴 수 없기 때문이다. 항목은 지도의 구조를 바꾼다.

이름은 그렇지 않다. `ScreenEntity.name` 은 표시값이고 조인 키가 아니다 — 아무도 이 값으로
행을 찾지 않고, 틀린 이름은 사람이 content map 에서 한 행을 잘못 읽는 것으로 끝나며 나중에
덮어쓸 수 있다. 그래서 여기서 형식 검사 위에 얹는 것은 **하나**뿐이다: 모델이 자기가 본
selector 문자열을 그대로 돌려준 경우.

그 하나를 보는 이유는 그것이 이 agent 의 실패 모드이기 때문이다. `discriminator` 는
기계 문자열 목록이고, 근거가 그것뿐일 때 모델은 화면을 부르는 대신 목록을 옮겨 적는다.
`Canvas/Panel/Continue` 는 이름이 아니라 복사다. 글자 그대로 같은 경우만 걸러낸다 —
"이 이름이 selector 목록을 풀어 쓴 것인가" 는 기계가 답할 수 있는 질문이 아니고, 답하는
척하면 멀쩡한 이름을 버리기 시작한다.

## 길이는 자르지 않고 버린다

상한을 넘긴 이름을 잘라 실으면 잘린 이름이 남는다. 잘린 이름은 틀린 이름이고, 이름 없는
`screen` 은 그냥 이름이 없을 뿐이다. 계약이 `null` 을 정상으로 두는 것이 이 선택을 공짜로
만든다.
"""

from app.agents.screen_name.schemas import ProposedName
from app.qa.envelope import MAX_SCREEN_NAME_LENGTH, ScreenSelectorScreenRef


def usable_name(
    proposed: ProposedName, screen: ScreenSelectorScreenRef
) -> tuple[str | None, str | None]:
    """실어 보낼 이름, 그리고 버렸으면 그 사유.

    사유가 `None` 인 `(None, None)` 은 버린 것이 아니라 **모델이 짓지 않기로 한 것**이다.
    둘을 나누는 이유는 `note` 에 적히는 문장이 달라야 하기 때문이다 — 전자는 근거가
    이름을 받치지 못했다는 답이고, 후자는 답이 있었는데 못 실었다는 기록이다.
    """
    if proposed.name is None:
        return None, None

    # 줄바꿈과 겹친 공백을 하나로 만든다. 한 줄짜리 표시값이라 개행이 들어가면 content map
    # 의 한 칸이 두 줄이 되고, 그것은 이름이 아니라 문단이다.
    name = " ".join(proposed.name.split())
    if not name:
        return None, "the model answered with a blank name"
    if len(name) > MAX_SCREEN_NAME_LENGTH:
        return None, (
            f"the name is longer than {MAX_SCREEN_NAME_LENGTH} characters: {name!r}"
        )
    if _is_a_selector_it_was_shown(name, screen):
        return None, f"the name is one of the selectors it was shown, not a name: {name!r}"
    return name, None


def _is_a_selector_it_was_shown(name: str, screen: ScreenSelectorScreenRef) -> bool:
    """이름이 `discriminator` 의 selector 하나를 글자 그대로 옮긴 것인가.

    두 조건을 **둘 다** 본다: selector 와 대소문자만 빼고 같고, 그 selector 가 기계
    문자열로만 보이는 모양이다 — `/` 나 `[` 나 `(` 를 하나라도 가졌다.

    두 번째 조건이 없으면 맞는 이름을 버린다. `discriminator` 에 `Shop` 하나만 있는
    화면의 이름으로 `Shop` 은 옮겨 적은 것이 아니라 바른 답이고, 사람도 그렇게 부른다.
    거르려는 것은 `UIRoot[0]/Dialog[1]/Btn[0]` 처럼 사람이 화면 이름으로 절대 쓰지 않는
    문자열 하나다.
    """
    folded = name.casefold()
    return any(
        entry.selector
        and entry.selector.strip().casefold() == folded
        and any(mark in entry.selector for mark in "/[(")
        for entry in screen.discriminator
    )
