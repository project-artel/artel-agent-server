"""이 런이 배운 것을 content map 에 적는 tool 셋이 모델에게 말하는 것, 그리고 그 답을 옮겨
적는 자리.

`app/agents/qa/knowledge.py` · `app/agents/qa/screen.py` 와 같은 자리다 — tool 설명이 사용
정책의 단일 출처라(ARTEL-192) `tools.py` 안에 두면 그 파일이 정책 문서가 된다.

## 무엇을 적는가

`capability` 한 행은 "이 씬에서 이것을 하면 이렇게 된다" 한 줄이고, 지도는 그것을 정적 분석이
읽어 낸 것으로 채운다. 그 행이 실제로 참인지는 거의 아무도 안 봤다 — `artel_integration` 실측
으로 472 행 중 `verification = 'confirmed'` 이 2 행이다.

기계로 확인하는 길은 막혀 있다. 472 행 중 418 행이 `interaction = 'none'` 이라 누를 것이 없고
("적을 처치하면 보상을 받는다" 처럼 누르는 것이 아니라 일어나는 것이다), action 전후의
`pulse` 를 비교하는 방식은 그 418 행에 대해 영영 아무 말도 못 한다(ARTEL-450). 일어났는지는
화면을 본 쪽이 안다. 그래서 이 tool 셋이 있다.

## 왜 설명이 이렇게 긴가

이 tool 들이 쓰는 것은 이 런의 기록이 아니라 **다음 런들이 읽을 지도**다. 틀린 행 하나는 이
런에서 아무 손해도 안 내고 다음 런의 테스트케이스가 된다. 그래서 설명이 지고 있는 것은 인자
사용법이 아니라 **선**이다 — 눌러 보고 본 것과 그럴듯하게 생각한 것을 가르는 선, 그리고 근거가
이미 적어 둔 것과 근거가 놓친 것을 가르는 선.
"""

from app.prompts import load_tool_description
from app.qa.envelope import CapabilityWriteResultPayload
from app.qa.scene_context import SceneCapability

# agent 의 판단. orchestration 의 `CapabilityVerdict` 와 글자까지 같다.
CAPABILITY_VERDICTS = ("works", "fails")

# agent 가 만들 수 있는 행의 출신. `evidence` 와 `human` 은 저쪽이 거절한다 — agent 가 정적
# 분석의 옷을 입은 행을 만들면 "이 행이 어디서 왔나" 가 답할 수 없는 질문이 된다.
CAPABILITY_ORIGINS = ("observed", "inferred")

# orchestration 의 `Interaction` 과 같은 다섯. 여섯째를 여기서 만들면 저쪽이 거절한다.
CAPABILITY_INTERACTIONS = ("click", "type", "press", "axis", "none")

# `press` 에만 곁들이는 것이 아니라 어느 조작에나 붙을 수 있는 선택값이다.
INPUT_PHASES = ("down", "held", "up")

# 저쪽 상한과 같은 값이고, 넘으면 거절된다 — 왕복 하나를 아끼려고 여기서도 본다.
MAX_RATIONALE_LENGTH = 2_000
MAX_SUMMARY_LENGTH = 1_000

# `list_scene_capabilities` 한 번이 내는 줄 수.
#
# 실측 `TurnBattleScene` 이 232 행(누를 수 있는 것 8, `not-a-step` 224)이라 한 번에 다 내면
# 그 답 하나가 런 내내 문맥에 앉는다. 20 줄이면 `contains` 로 좁힌 답은 거의 언제나 한 번에
# 들어오고, 안 좁힌 답도 화면 하나를 안 밀어낸다.
CAPABILITY_PAGE = 20

def capability_tool_description(tool_name: str) -> str:
    """What the model reads for one capability tool, from `qa_run/<version>/tool_<tool_name>.md`.

    The length caps and the interaction list are filled from the constants above, so
    the numbers the agent is told are the ones this side enforces.
    """
    return load_tool_description(tool_name).body.format(
        rationale_max=MAX_RATIONALE_LENGTH,
        summary_max=MAX_SUMMARY_LENGTH,
        interactions=", ".join(CAPABILITY_INTERACTIONS),
        page=CAPABILITY_PAGE,
    )


# 답이 안 온 경우에 붙이는 말. 지식 쓰기의 `UNCONFIRMED_WRITE` 와 같은 판단이다 — 침묵을
# 실패로 옮겨 적으면 모델이 같은 문장을 다시 보낸다.
UNCONFIRMED_CAPABILITY_WRITE = (
    "No answer came back, so this side cannot say whether the content map took it. "
    "It may well have. Do not send the same thing again — carry on with the step."
)


def render_capability_write_result(payload: CapabilityWriteResultPayload) -> str:
    """받아들여진 쓰기 하나를 모델이 읽는 문장으로.

    `observation_id` 를 반드시 말한다. `inferred` 를 적을 때 `based_on` 에 실을 수 있는 값이
    이것뿐이고, 안 말하면 모델은 딛고 설 것이 없어 추론을 아예 못 적거나 — 더 나쁘게 —
    지어낸 id 를 싣는다.

    `created` 가 false 인 것을 감추지 않는다. 재전송이 흡수된 것과 새 행이 생긴 것은 다른
    일이고, 뭉개면 모델은 자기가 방금 무엇을 만들었다고 믿는다.
    """
    lines: list[str] = []
    if payload.capability_key:
        target = f"capability {payload.capability_id} [{payload.capability_key}]"
    else:
        target = f"capability {payload.capability_id}"

    if payload.created:
        lines.append(f"The content map wrote a new row: {target}.")
    elif payload.type == "CAPABILITY_DISCOVERED":
        lines.append(
            f"The content map already held this one, so nothing new was written: {target}. "
            "Its stored description was left exactly as it was."
        )
    else:
        lines.append(f"The content map took your verdict on {target}.")

    if payload.verification:
        lines.append(f"That row now reads `verification: {payload.verification}`.")

    if payload.observation_id:
        lines.append(
            f"This statement was stored as observation {payload.observation_id}. That is "
            "the id to put in `based_on` if you later infer something from it."
        )
    if not payload.capability_key and payload.capability_id:
        lines.append(
            f"This row has no key, so name it as `capability_id` {payload.capability_id} "
            "if you come back to it with a verdict."
        )
    return " ".join(lines)


def _matches(capability: SceneCapability, needle: str) -> bool:
    """`contains` 가 한 줄에 걸리는가.

    요약뿐 아니라 `given_text` · `control_label` · `control_path` 도 본다. 모델이 들고 오는
    말은 대개 자기가 방금 누른 것의 이름이고, 그 이름은 요약이 아니라 label 이나 path 에
    적혀 있다.
    """
    haystack = " ".join(
        part
        for part in (
            capability.summary,
            capability.given_text,
            capability.control_label,
            capability.control_path,
        )
        if part
    )
    return needle in haystack.lower()


def capability_search_line(capability: SceneCapability) -> str:
    """`list_scene_capabilities` 의 한 줄.

    키가 없으면 `id=` 로 낸다. 그 행은 이 런이나 이전 런의 agent 가 만든 것이고, verdict 를
    찍는 유일한 길이 id 라 키 자리를 비워 두면 모델이 지목할 수 없다.

    `happens` 를 붙이는 것은 `status` 가 `not-a-step` 인 줄이다. 누를 수 없다는 사실이 그
    줄에서 가장 중요한 정보다 — 그것을 안 말하면 모델이 없는 컨트롤을 찾는다.
    """
    head = (
        f"[{capability.capability_key}]"
        if capability.capability_key
        else f"[id={capability.capability_id}]"
    )
    what = capability.interaction or "?"
    if capability.status == "not-a-step":
        what = "happens"
    if capability.input_key:
        what = f"{what} {capability.input_key}"
    if capability.control_label:
        what = f'{what} "{capability.control_label}"'
    parts = [head, what]
    if capability.summary:
        parts.append(f"— {capability.summary}")
    if capability.given_text:
        parts.append(f"| given: {capability.given_text}")
    if capability.verification:
        parts.append(f"[{capability.verification}]")
    return "  " + " ".join(parts)


def capability_search_matches(
    capabilities: list[SceneCapability], contains: str
) -> list[SceneCapability]:
    """`contains` 에 걸리는 줄 전부, 지도가 준 순서 그대로."""
    needle = contains.strip().lower()
    return [item for item in capabilities if not needle or _matches(item, needle)]


def capability_search_page(
    matched: list[SceneCapability], offset: int
) -> list[SceneCapability]:
    """한 번의 조회가 실제로 찍는 줄.

    자르는 규칙이 한 자리에 있어야 한다. `report_step` 의 `capability_key` 검사가 "이 런이
    실제로 받은 줄" 을 묻는데, 그 답을 렌더 함수 안에서 한 번 더 계산하면 두 규칙이 어긋나는
    날 검사가 안 받은 키를 받았다고 하거나 그 반대가 된다.
    """
    start = max(offset, 0)
    return matched[start : start + CAPABILITY_PAGE]


def render_capability_search(
    scene: str,
    capabilities: list[SceneCapability],
    contains: str,
    offset: int,
) -> str:
    """한 씬의 capability 를 걸러 한 페이지 낸다.

    빈 결과를 사실대로 말하고 그것이 무엇을 뜻하는지도 말한다. 지도에 없다는 것은 지도가
    놓쳤다는 뜻이지 그런 일이 안 일어난다는 뜻이 아니다 — 그 구분이 이 tool 과
    `record_new_capability` 사이의 갈림길이다.
    """
    needle = contains.strip().lower()
    matched = capability_search_matches(capabilities, contains)
    if not matched:
        if needle:
            return (
                f"Nothing in {scene}'s {len(capabilities)} mapped capabilities mentions "
                f"{contains.strip()!r}. That means the map never recorded it, not that it "
                "does not happen — if you watched it, `record_new_capability` is where it "
                "goes."
            )
        return f"The content map holds no capabilities for {scene}."

    start = max(offset, 0)
    page = capability_search_page(matched, offset)
    if not page:
        return (
            f"`offset` {start} is past the end of the list — {len(matched)} line(s) "
            f"matched in {scene}."
        )

    shown_to = start + len(page)
    heading = (
        f"{scene}: {len(matched)} capability line(s) match, showing {start + 1}-{shown_to}"
        if needle
        else f"{scene}: {len(matched)} mapped capability line(s), showing {start + 1}-{shown_to}"
    )
    lines = [f"{heading}:", *(capability_search_line(item) for item in page)]
    if shown_to < len(matched):
        lines.append(
            f"  ({len(matched) - shown_to} more — call again with offset={shown_to}, or "
            "narrow it with `contains`)"
        )
    lines.append(
        "  (this is the map, not the screen. Ids and coordinates to act on come from the "
        "scene view; the key in brackets is what `record_capability_verdict` takes)"
    )
    return "\n".join(lines)
