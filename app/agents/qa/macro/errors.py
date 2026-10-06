"""macro 가 거절하거나 멈추는 자리의 코드와 예외 둘.

거절이 두 층이다. 하나는 **저장 시점**이고, 그것이 `MacroRejection` 이다. 저장 때
잡는 것이 런타임에 잡는 것보다 언제나 낫다 — 런타임에 걸리면 이미 게임에 나간 action
은 되돌릴 수 없다. 다른 하나는 **실행 시점**이고 그것이 `MacroFailure` 다. 값은 돌 때
도착하므로 저장 때 알 수 없는 것이 남는다.

코드에 `MACRO_` 접두를 붙이지 않는다. `REQUIRE_FAILED` 이지
`MACRO_REQUIRE_FAILED` 가 아니다. 저장 시점 거절 하나만 `MACRO_NODE_REJECTED` 이고,
그것은 접두가 아니라 그 코드의 이름 전체다.
"""

from dataclasses import dataclass, field
from typing import Any

# 저장 시점 거절. parser 가 허용 node 목록에 없는 node 를 만났거나, 타입·이름·상한
# 규칙을 어겼을 때. `register_macro` 는 이 코드로 거절하고 아무것도 등록하지 않는다.
MACRO_NODE_REJECTED = "MACRO_NODE_REJECTED"

# 조건이 거짓이었다. 게임이 다르게 동작한 것이고, agent 는 결함을 적을지 판단한다.
REQUIRE_FAILED = "REQUIRE_FAILED"
# 조건이 거짓인데 아직 아무 action 도 안 나갔다. 들어선 자리가 틀렸다는 말이다.
SCENE_MISMATCH = "SCENE_MISMATCH"
# 환경이나 호출이 틀렸다. step 을 실패로 적지 않는 코드다.
ACTION_REJECTED = "ACTION_REJECTED"
# macro 텍스트에 적힌 조회가 지금 아무것도 안 잡는다. 정의를 고칠 일이다.
SELECTOR_NOT_FOUND = "SELECTOR_NOT_FOUND"
# 같은 글자를 띄운 객체가 둘 이상이다. 서버가 임의로 하나를 고르지 않는다.
SELECTOR_AMBIGUOUS = "SELECTOR_AMBIGUOUS"
# 이 런에서 몇 statement 전에 푼 값이 그 사이에 죽었다. 다시 부르면 되는 일이고,
# `SELECTOR_NOT_FOUND` 와 섞으면 agent 가 할 일이 뒤바뀐다.
STALE_BINDING = "STALE_BINDING"
# macro 가 물을 수 없는 것을 물었다. 게임이 틀린 것이 아니라 macro 정의가 틀린 것이라
# `REQUIRE_FAILED` 와 같은 코드에 담을 수 없다 — 담으면 agent 가 macro 의 잘못을
# 게임의 결함으로 적는다. step 을 실패로 적지 않는다.
COMPARISON_REJECTED = "COMPARISON_REJECTED"

# action 이 나갔는데 화면이 연달아 몇 번 그대로였다. 일곱 중에 맞는 것이 없어 새로
# 둔다. `REQUIRE_FAILED` 는 저자가 적은 조건이 거짓이었다는 뜻인데 여기에는 적힌 조건이
# 없고, `ACTION_REJECTED` 는 step 을 실패로 적지 않는 코드인데 멈춘 화면은 QA 가 찾는
# 결함 그 자체일 수 있다. 어느 쪽인지는 agent 가 정한다.
SCREEN_UNCHANGED = "SCREEN_UNCHANGED"
# macro 가 도는 동안 operator 가 말을 걸었다. 게임의 잘못도 macro 의 잘못도 아니라
# 둘 중 어느 코드에도 담을 수 없다. step 을 실패로 적지 않는다.
OPERATOR_INTERRUPTED = "OPERATOR_INTERRUPTED"

RUNTIME_CODES = (
    REQUIRE_FAILED,
    SCENE_MISMATCH,
    ACTION_REJECTED,
    SELECTOR_NOT_FOUND,
    SELECTOR_AMBIGUOUS,
    STALE_BINDING,
    COMPARISON_REJECTED,
    SCREEN_UNCHANGED,
    OPERATOR_INTERRUPTED,
)


class MacroRejection(Exception):
    """저장 시점 거절. 받을 수 있는 것을 `reason` 이 이름으로 전부 대야 한다.

    `line` 은 거절한 node 의 줄 번호다. 모델이 고칠 자리를 찾는 데 쓰이고, `ast` node
    에 없으면 비운다.
    """

    code = MACRO_NODE_REJECTED

    def __init__(self, reason: str, line: int | None = None) -> None:
        self.reason = reason
        self.line = line
        super().__init__(reason if line is None else f"line {line}: {reason}")

    def render(self) -> str:
        """모델이 읽는 한 문단. 어느 줄이 문제였는지와 무엇을 고치면 되는지."""
        where = "" if self.line is None else f" (line {self.line})"
        return f"{self.code}{where} — {self.reason}"


@dataclass
class MacroFailure(Exception):
    """실행 시점 멈춤. `code` 는 `RUNTIME_CODES` 중 하나다.

    `payload` 는 그 코드가 기대·관측·되는 연산자 같은 것을 싣는 자리다. 코드마다 싣는
    것이 달라 사전으로 둔다 — 코드마다 필드를 다 합친 하나의 모양을 만들면 어느
    코드에서 어느 칸이 뜻을 갖는지 읽는 쪽이 모른다.
    """

    code: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code} — {self.reason}")
