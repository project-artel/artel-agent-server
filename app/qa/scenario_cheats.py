"""시나리오 단계에 적힌 게임 기능(치트)을 planner 를 거치지 않고 엔진이 부른다.

시나리오 작성 안내는 상태를 맞추는 단계를 "게임 기능 `set_progress(resumeStage=1, unlockedStage=24)` 로 …" 처럼
적게 한다. planner 의 계획 문법에는 치트가 없어서 이런 단계는 어떤 계획으로도 할 수 없다. 그래서 단계 글에서
호출을 읽어 그대로 보낸다. 판정이 없는 단계(`case_id` 없음)에만 쓴다.

호출만 있는 단계는 보내고 끝낸다. 호출 뒤에 조작이 이어지면("… 로 Stage 10 을 연 뒤, 지지를 클릭해 …")
호출을 보낸 뒤 그 단계 글에 "이미 실행했다" 를 붙여 나머지만 planner 가 쓰게 한다.
"""

import re
from dataclasses import dataclass, field
from typing import Any

# "게임 기능 go_to_stage(stage=10)" — 이름과 괄호 안 인자.
_CALL = re.compile(r"게임 기능\s+([A-Za-z_]\w*)\s*\(([^()]*)\)")
# "게임 초기화(reset_game)" — 인자 없는 내장 치트.
_RESET = re.compile(r"게임 초기화\s*\(\s*(reset_game)\s*\)")
# 호출을 뺀 나머지가 이 모양이면 그 단계는 호출뿐이다: "으로 새 게임 진행 상태를 저장한다", "로 Stage 4 를 연다".
_ONLY_CALL_TAIL = re.compile(r"^\s*(으로|로)?\s*[^,]*?(저장한다|연다|불러온다|맞춘다)\.?\s*$")

ALREADY_RAN = "(엔진이 이미 실행했다 — 다시 하지 않는다)"


@dataclass(frozen=True)
class ScenarioCheat:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    # 단계 글에서 이 호출이 적힌 부분. "이미 실행했다" 를 붙일 자리다.
    phrase: str = ""


def cheats_in(text: str) -> list[ScenarioCheat]:
    """단계 글에 적힌 호출을 적힌 순서대로."""
    found: list[tuple[int, ScenarioCheat]] = []
    for match in _CALL.finditer(text):
        found.append((match.start(), ScenarioCheat(match.group(1), _arguments(match.group(2)), match.group(0))))
    for match in _RESET.finditer(text):
        found.append((match.start(), ScenarioCheat("reset_game", {}, match.group(0))))
    return [cheat for _start, cheat in sorted(found, key=lambda pair: pair[0])]


def only_cheats(text: str, cheats: list[ScenarioCheat]) -> bool:
    """호출을 빼면 남는 조작이 없나."""
    rest = text
    for cheat in cheats:
        rest = rest.replace(cheat.phrase, " ", 1)
    return _ONLY_CALL_TAIL.match(rest.strip()) is not None


def with_cheats_done(text: str, cheats: list[ScenarioCheat]) -> str:
    """planner 가 읽을 단계 글. 보낸 호출마다 "이미 실행했다" 를 붙인다."""
    for cheat in cheats:
        text = text.replace(cheat.phrase, f"{cheat.phrase}{ALREADY_RAN}", 1)
    return text


def _arguments(source: str) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    for part in source.split(","):
        if "=" not in part:
            continue
        key, raw = (piece.strip() for piece in part.split("=", 1))
        if key:
            arguments[key] = _value(raw)
    return arguments


def _value(raw: str) -> Any:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    for kind in (int, float):
        try:
            return kind(raw)
        except ValueError:
            pass
    return raw
