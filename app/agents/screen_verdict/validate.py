"""모델이 내놓은 항목 중 프레임에 실어도 되는 것만 남긴다.

## 왜 형식 검사만으로 부족한가

`match` 가 셋 중 하나이고 `pattern` 이 512자 이하인 것은 orchestration 도 본다. 여기서 더
보는 것은 **그 `pattern` 이 이 제안이 물어본 것을 가리키는가** 하나다.

이유는 저쪽의 검사가 "이 `scene` 에서 본 적 있는가" 이기 때문이다. 그 검사는 제안이 물어본
적 없는 selector 도 통과시킨다 — 같은 `scene` 에 있기만 하면 된다. 그런데 물어본 적 없는
selector 에 대한 답은 아무도 요청하지 않은 판정이고, 저쪽의 "각 `(scene, selector)` 는
평생 한 번만 묻는다" 장부에도 그 항목이 없다. 지어낸 문자열이 우연히 그 `scene` 의 무언가와
같으면 그대로 앉는다.

## 버리는 쪽으로 틀린다

검사가 멀쩡한 항목을 버릴 수는 있다. 그래도 그 방향으로 둔다 — **기본값이 무시**라
버려진 항목은 지도를 종전대로 두지만, 잘못 앉은 항목은 그 `scene` 의 화면을 다음 관측부터
갈라 놓고 그것을 되돌리는 항목도 이미 갈라진 과거를 복원하지 못한다.

`subtree` 의 조상 검사도 그래서 마디 경계에서만 맞춘다. index 를 지우는 규칙을 여기서 다시
구현하지 않는 것도 같은 판단이다 — 그 규칙은 이미 Kotlin 과 SQL 에 두 벌 있고, 세 번째
벌이 어긋나는 순간 화면이 조용히 갈린다. 후보가 자기 `selector` 와 `path` 를 둘 다 싣고
오므로 여기서는 그 둘과 글자로 맞대 보면 된다.
"""

from dataclasses import dataclass

from app.agents.qa.screen import (
    MAX_PATTERN_LENGTH,
    PATH_MATCH,
    SCREEN_SELECTOR_MATCHES,
    SELECTOR_MATCH,
    SUBTREE_MATCH,
)
from app.agents.screen_verdict.schemas import ProposedEntry
from app.qa.envelope import ScreenSelectorCandidate, ScreenSelectorEntry


@dataclass(frozen=True)
class DroppedEntry:
    """버린 항목 하나와 그 사유.

    사유를 들고 다니는 것은 이것이 판정의 품질을 읽는 유일한 창이기 때문이다. 모델이 자꾸
    후보 밖을 가리키는지, 형식을 어기는지, 서로 어긋나는 답을 내는지는 로그에서만 보인다.
    """

    entry: ProposedEntry
    reason: str


def usable_entries(
    proposed: list[ProposedEntry], candidates: list[ScreenSelectorCandidate]
) -> tuple[list[ScreenSelectorEntry], list[DroppedEntry]]:
    """실어 보낼 항목과 버린 항목.

    후보가 하나도 없는 제안이면 전부 버린다. 물어본 것이 없는데 답이 있다는 것은 모델이
    지어냈다는 뜻이다.
    """
    dropped: list[DroppedEntry] = []
    # 대상 `(match, pattern)` 마다, 그 대상을 가리킨 항목들. 같은 대상에 두 번 답하는 것
    # 자체는 무해하지만 **서로 반대로 답하는 것은 무해하지 않다** — 어느 쪽을 고르든
    # 절반은 모델이 하지 않은 판정이 된다. 그때는 그 대상을 통째로 버린다.
    by_target: dict[tuple[str, str], list[ProposedEntry]] = {}

    for entry in proposed:
        target = _target_of(entry, candidates)
        if isinstance(target, str):
            dropped.append(DroppedEntry(entry, target))
            continue
        by_target.setdefault(target, []).append(entry)

    kept: list[ScreenSelectorEntry] = []
    for (kind, pattern), answers in by_target.items():
        verdicts = {answer.screen_defining for answer in answers}
        if len(verdicts) > 1:
            dropped.extend(
                DroppedEntry(answer, "the same target was answered both ways")
                for answer in answers
            )
            continue
        first, *rest = answers
        dropped.extend(
            DroppedEntry(answer, "the same target was already answered")
            for answer in rest
        )
        kept.append(
            ScreenSelectorEntry(
                match=kind,
                pattern=pattern,
                screen_defining=first.screen_defining,
                reason=first.reason.strip(),
            )
        )

    return kept, dropped


def _target_of(
    entry: ProposedEntry, candidates: list[ScreenSelectorCandidate]
) -> tuple[str, str] | str:
    """`(match, pattern)` 정규형, 아니면 버리는 사유 문자열."""
    kind = (entry.match or "").strip().lower()
    if kind not in SCREEN_SELECTOR_MATCHES:
        return f"match is not one of {', '.join(SCREEN_SELECTOR_MATCHES)}: {entry.match!r}"

    pattern = (entry.pattern or "").strip()
    if not pattern:
        return "pattern is empty"
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"pattern is longer than {MAX_PATTERN_LENGTH} characters"
    if not (entry.reason or "").strip():
        return "reason is empty"
    if not _points_at_a_candidate(kind, pattern, candidates):
        return f"pattern does not name anything this proposal asked about: {pattern!r}"
    return kind, pattern


def _points_at_a_candidate(
    kind: str, pattern: str, candidates: list[ScreenSelectorCandidate]
) -> bool:
    if kind == SELECTOR_MATCH:
        return any(pattern == candidate.selector for candidate in candidates)
    if kind == PATH_MATCH:
        return any(pattern == candidate.path for candidate in candidates)
    if kind == SUBTREE_MATCH:
        return any(_covers(pattern, candidate.path) for candidate in candidates)
    return False


def _covers(pattern: str, path: str) -> bool:
    """`pattern` 이 `path` 자신이거나 그 조상인가. 마디 경계로만 맞춘다.

    이것이 `subtree` 가 정규식 없이도 "이 아래 전부" 를 말할 수 있는 이유이고, 마디로
    끊는 이유는 `Zone1` 이 `SomeZone1Extra` 에 걸리면 안 되기 때문이다.
    """
    if not pattern or not path:
        return False
    head = pattern.split("/")
    whole = path.split("/")
    return len(head) <= len(whole) and whole[: len(head)] == head
