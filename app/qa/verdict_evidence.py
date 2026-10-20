"""판정 하나의 근거와, 그 근거를 판독 기록과 대조하는 일(ARTEL-1133).

QA 판정은 모델이 내린다. 사용자가 그 판정을 믿으려면 **왜**를 볼 수 있어야 하고, 그 "왜"가 모델의
말이 아니라 기록에 있는 사실이어야 한다. 그래서 판정마다 기대 항목별로 무엇을 보았는지(`checks`)를
받고, 근거로 댄 판독 값 · 캡처 · 씬을 이 런의 기록과 맞춰 본다.

대조는 근거가 **사실인가**를 본다. 기대 결과가 일어났는가를 코드가 따로 판단하지는 않는다 —
그것은 기대 항목마다 확인할 이름이 있어야 하는 다음 일이다.

실측(2026-10-20, 실제 게임 시도의 검증 판정 50건): 판정이 사유에 든 사실 중 13건은 기록과 일부만
맞았고 1건은 틀렸다 — 다른 적 개체의 HP 를 인용하거나, 한 번도 읽지 않은 값을 댔다.
"""

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.qa.pulse import PulseMemory

Met = Literal["yes", "no", "unknown"]
Citation = Literal["match", "mismatch", "missing"]
Evidence = Literal["verified", "uncertain", "none"]


class EvidenceSource(BaseModel):
    """판정이 근거로 댄 것 하나."""

    kind: Literal["pulse", "capture", "scene"] = Field(
        description="pulse: an object (or one of its values) in the pulse view · capture: a screenshot · scene: ONLY the scene name"
    )
    object: str | None = Field(
        default=None,
        description="pulse: the object's path or selector as the view prints it (with no member, its presence or text is checked) · capture: the capture id · scene: the scene name",
    )
    member: str | None = Field(
        default=None, description="pulse only: the member, `Type::member` or `member`, as the view prints it"
    )
    value: str | None = Field(default=None, description="pulse only: the value you read, as printed")


class VerdictCheck(BaseModel):
    """기대 항목 하나: 무엇을 기대했고, 무엇을 보았고, 그래서 충족했는가."""

    expected: str = Field(description="One expected item, in the step's own words")
    observed: str = Field(description="What you actually saw for it")
    met: Met = Field(description="yes / no / unknown — unknown when nothing you can read shows it")
    sources: list[EvidenceSource] = Field(default_factory=list, description="What shows it")


def _plain(text: str) -> str:
    """대조용 이름: 인덱스(`[7]`) · 대소문자 · 양끝 공백을 걷는다. 화면 표기와 판독 표기가 다르게 붙는다."""
    return re.sub(r"\[\d+\]", "", text).strip().strip("/").lower()


def _shown(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _same(cited: str, recorded: Any) -> bool:
    """인용한 값이 기록과 맞는가. 기록이 객체면 그 안의 글자 하나와 맞아도 된다(`{"label": "Stage : 0"}`)."""
    want = cited.strip().strip("\"'").lower()
    if isinstance(recorded, dict):
        return any(_same(cited, value) for value in recorded.values()) or want == _shown(recorded).lower()
    got = _shown(recorded).strip().strip("\"'").lower()
    return want == got


def _names_object(cited: str, *names: str | None) -> bool:
    plain = _plain(cited)
    return any(name and (_plain(name) == plain or _plain(name).endswith("/" + plain)) for name in names)


def _names_member(cited: str, on: str | None, member: str | None) -> bool:
    kind, _, name = cited.rpartition("::")
    if (member or "").lower() != name.strip().lower():
        return False
    return not kind or (on or "").lower().endswith(kind.strip().lower())


def _pulse_values(source: EvidenceSource, pulse: PulseMemory) -> list[Any] | None:
    """그 객체 · member 의 기록 값들. 그런 것이 기록에 없으면 None."""
    found: list[Any] = []
    if source.object:
        for held in pulse.held.values():
            if not _names_object(source.object, held.selector, held.path):
                continue
            if source.member is None:
                found.append(held.text if held.text is not None else held.live)
                continue
            found.extend(m.value for m in held.members.values() if _names_member(source.member, m.on, m.member))
    elif source.member:
        found.extend(s.value for s in pulse.statics.values() if _names_member(source.member, s.declaring, s.member))
        for held in pulse.held.values():
            found.extend(m.value for m in held.members.values() if _names_member(source.member, m.on, m.member))
    return found or None


def cite(
    checks: list[VerdictCheck], pulse: PulseMemory, captures: set[str], scene: str | None
) -> tuple[list[dict[str, Any]], Evidence]:
    """근거마다 기록과 맞춰 본 결과와, 판정 전체의 근거 상태.

    `verified` 는 기대 항목마다 근거가 하나 이상 있고 그 전부가 기록과 맞을 때뿐이다. 근거 없는 항목,
    기록과 어긋나거나 기록에 없는 근거가 하나라도 있으면 `uncertain` 이다.
    """
    out: list[dict[str, Any]] = []
    certain = bool(checks)
    for check in checks:
        sources = []
        for source in check.sources:
            recorded: Any = None
            if source.kind == "capture":
                citation: Citation = "match" if (source.object or "") in captures else "missing"
                recorded = source.object if citation == "match" else None
            elif source.kind == "scene" and (scene is None or _plain(source.object or "") == _plain(scene)
                                             or _pulse_values(source, pulse) is None):
                # 실측(2026-10-20 런 18): 모델이 화면 위 오브젝트를 `scene` 으로 적었다. 씬 이름이 아니고 판독
                # 기록에 있는 오브젝트면 아래 `pulse` 갈래로 오브젝트를 대조한다.
                recorded = scene
                citation = "missing" if scene is None else "match" if _plain(source.object or "") == _plain(scene) else "mismatch"
            else:
                values = _pulse_values(source, pulse)
                if values is None:
                    citation = "missing"
                elif source.value is None or any(_same(source.value, value) for value in values):
                    citation = "match"
                    recorded = values[0] if source.value is None else next(v for v in values if _same(source.value, v))
                else:
                    citation = "mismatch"
                    recorded = values[0]
            certain = certain and citation == "match"
            sources.append({
                "kind": source.kind,
                "key": "::".join(part for part in (source.object, source.member) if part) or None,
                "value": source.value,
                "recorded": None if recorded is None else _shown(recorded),
                "citation": citation,
            })
        certain = certain and bool(sources)
        out.append({"expected": check.expected, "observed": check.observed, "met": check.met, "sources": sources})
    status: Evidence = "none" if not checks else "verified" if certain else "uncertain"
    return out, status


def contradiction(passed: bool, checks: list[VerdictCheck]) -> str | None:
    """판정과 항목이 서로 반대면 그 사정. 실측에서 사유에는 "드래그 실패"라 쓰고 통과로 낸 판정이 있었다."""
    unmet = [check.expected for check in checks if check.met == "no"]
    if passed and unmet:
        return f"you marked this step passed, but these expected items are `met: no`: {unmet}"
    if not passed and checks and all(check.met == "yes" for check in checks):
        return "you marked this step failed, but every expected item is `met: yes`"
    return None
