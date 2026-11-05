"""run 이 예외로 멈춘 자리를 ERROR frame 에 실을 만큼만 추린다.

예외 메시지에는 게임 데이터나 사용자 입력이 섞일 수 있고, 파일 경로에는 설치 환경의
디렉터리 이름이 들어 있다. 그래서 클래스 이름과 `app` 패키지 안의 `module:function:line`
만 남기고 나머지는 버린다.
"""

from dataclasses import dataclass
from types import TracebackType
import traceback

MAX_FRAMES = 10
_PACKAGE_PREFIX = "app."


@dataclass(frozen=True)
class FailureSite:
    exception_type: str
    frames: list[str]


def failure_site_of(error: BaseException) -> FailureSite:
    return FailureSite(
        exception_type=type(error).__name__,
        frames=app_frames(error.__traceback__),
    )


def app_frames(traceback_tail: TracebackType | None) -> list[str]:
    """`app.` 모듈의 frame 만, 가장 안쪽 것을 포함해 최대 10개를 바깥에서 안쪽 순으로 돌려준다."""
    frames: list[str] = []
    for frame, line_number in traceback.walk_tb(traceback_tail):
        module = frame.f_globals.get("__name__", "")
        if not isinstance(module, str) or not module.startswith(_PACKAGE_PREFIX):
            continue
        frames.append(f"{module}:{frame.f_code.co_name}:{line_number}")
    return frames[-MAX_FRAMES:]
