"""판정 근거 기록(`StepPlay.evidence`)을 이름마다 바뀐 지점의 열로 펼친다. evaluator 의 글과 `evidence` 로그가 같은 열을 쓴다.

기록 하나는 `{"at", "state_version", "values", "clipped"?, "omitted"?}` 이거나 읽지 못했으면 `{"at", "error"}` 다. 한 기록에
이름이 없을 때 그 기록이 완전했으면(잘림·서버 생략 없음) `(absent)`, 아니면 `(not read)` 다 — 상한에 걸려 빠진 객체를 부재로
읽으면 살아 있는 적이 죽은 것으로 보인다(STATE_SET 계약 52 행).
"""

import json
from typing import Any

from app.qa.state_set import INACTIVE, PRESENT, STALE, UNKNOWN

ABSENT = "(absent)"
NOT_READ = "(not read)"
# 값 대신 들어가는 상태 글. 따옴표 없이 그대로 보인다.
_STATUS_WORDS = {PRESENT, INACTIVE, STALE, UNKNOWN, ABSENT, NOT_READ}
# `evidence` 로그 하나의 상한. Orchestration 의 socket 은 64 KiB 를 넘는 frame 을 닫는다.
MAX_EVIDENCE_LOG_BYTES = 48_000


def readings(evidence: list[dict]) -> list[dict]:
    """값을 읽은 기록만."""
    return [entry for entry in evidence if "values" in entry]


def names_of(read: list[dict]) -> list[str]:
    """기록에 나온 이름, 처음 나온 순서대로."""
    names: list[str] = []
    for entry in read:
        names.extend(name for name in entry["values"] if name not in names)
    return names


def shown(value: Any) -> str:
    if isinstance(value, str) and (value in _STATUS_WORDS or value.startswith("(unread:")):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def points_of(name: str, read: list[dict]) -> list[tuple[str, str]]:
    """`[(at, 보인 값)]`: 첫 기록과, 값이 바뀐 기록. 빠진 이름은 그 기록이 완전했는지에 따라 부재와 미확인으로 갈린다."""
    points: list[tuple[str, str]] = []
    for entry in read:
        if name in entry["values"]:
            value = shown(entry["values"][name])
        else:
            value = NOT_READ if entry.get("clipped") or entry.get("omitted") else ABSENT
        if not points or points[-1][1] != value:
            points.append((entry["at"], value))
    return points


def evidence_log(evidence: list[dict], unknown: list[str]) -> dict[str, Any]:
    """`evidence` 로그의 본문: 이름마다 바뀐 지점, 읽은 수, 완전하지 않았던 기록, 읽지 못한 기록. 한 frame 에 안 들어가면
    뒤의 이름부터 빼고 뺀 수를 적는다."""
    read = readings(evidence)
    body: dict[str, Any] = {
        "readings": len(read),
        "series": {name: [list(point) for point in points_of(name, read)] for name in names_of(read)},
        "incomplete": [
            {"at": entry["at"], "clipped": entry.get("clipped", 0), "omitted": entry.get("omitted", 0)}
            for entry in read
            if entry.get("clipped") or entry.get("omitted")
        ],
        "failed": [{"at": entry["at"], "error": entry["error"]} for entry in evidence if "error" in entry],
        "unknown_members": unknown,
    }
    series = body["series"]
    dropped = 0
    while series and len(json.dumps(body, ensure_ascii=False).encode()) > MAX_EVIDENCE_LOG_BYTES:
        series.pop(next(reversed(series)))
        dropped += 1
        body["names_left_out"] = dropped
    return body
