"""action batch 하나가 끝나고 남는 것. 모델이 읽는 문장과, 문장이 되기 전의 데이터.

`ToolContext.run` 은 문장만 돌려준다. 모델이 그 문장을 읽고 다음을 정하기 때문이고,
tool 열여섯이 전부 그 문장을 그대로 반환한다.

macro runner 는 다르다. **모델 턴을 안 쓰는 것이 macro 의 존재 이유라**, 문장을 읽어
줄 사람이 batch 사이에 없다. 그래서 runner 는 자기가 판단해야 하는데, 문장을 정규식으로
긁으면 표현이 곧 프로토콜이 된다 — 문구 한 줄을 고치면 runner 가 조용히 안 멈춘다.
한국어 문장을 wire 에 실었다가 받는 쪽 계약을 깨뜨린 ARTEL-777 과 같은 실수다.

그래서 `ToolContext.act` 가 문장과 데이터를 함께 돌려준다. 문장은 모델이 읽고, 아래
세 필드는 runner 가 읽는다. 둘이 같은 관측에서 나오므로 어긋날 수 없다.
"""

from dataclasses import dataclass
from enum import StrEnum


class PressLanding(StrEnum):
    """누름 하나가 무엇에 닿았나. `_press_outcome` 이 문장으로 옮기기 전의 값이다.

    SDK 가 보내는 것은 `reached` 와 `pointerHeldByPerson` 두 값뿐이고, 이 셋은 그
    둘의 조합에 이름을 붙인 것이다.
    """

    # `reached` 에 이름이 있다. **보냈다는 뜻이지 받아서 무엇을 했다는 뜻이 아니다** —
    # SDK 는 `SendMessage` 를 `DontRequireReceiver` 로 부른다.
    sent = "sent"
    # `reached` 가 비었다. 그 자리에 누를 것이 없었다.
    reached_nothing = "reached_nothing"
    # 사람이 마우스를 쥐고 있어 가상 포인터가 전해지지 않았다. 조준을 고쳐도 소용없다.
    held_by_person = "held_by_person"


class ScreenChange(StrEnum):
    """이 batch 뒤에 화면이 움직였나. 셋인 이유는 `모름` 이 `안 움직였다` 가 아니라서다."""

    # 이 `action` 뒤에 `pulse` 가 새로 왔다.
    moved = "moved"
    # `pulse` 가 흐르는데 이 `action` 뒤에 새로 온 것이 없다. SDK 는 움직인 것이 없으면
    # `pulse` 를 아예 내지 않으므로 침묵이 곧 "그대로" 다(ARTEL-516).
    still = "still"
    # `pulse` 를 한 번도 못 봤거나 게임이 결과를 아예 안 줬다. 화면이 멈춘 것과 섞으면
    # 화면을 안 보내는 빌드에서 멀쩡한 macro 가 전부 멈춘다.
    unknown = "unknown"


@dataclass(frozen=True)
class ActionOutcome:
    """`ToolContext.act` 가 돌려주는 것 전부.

    `text` 는 종전 `run` 이 돌려주던 바로 그 문장이고, 화면과 operator 의 말이 이미
    붙어 있다. 나머지 셋은 그 문장을 만들 때 손에 있던 데이터다.
    """

    text: str
    screen: ScreenChange = ScreenChange.unknown
    # 이 batch 안의 누름 결과. `click` 은 둘(`mouse_down`·`mouse_up`), `drag` 은 둘,
    # `move_pointer` 는 하나도 없다. 누름이 아닌 결과는 여기 안 든다.
    landings: tuple[PressLanding, ...] = ()
    # 이 batch 가 도는 동안 operator 가 한 말. `text` 끝에도 같은 말이 붙어 있다 —
    # 여기 따로 드는 것은 runner 가 문장을 안 읽고 "말이 왔다" 를 알기 위해서다.
    operator_messages: tuple[str, ...] = ()
