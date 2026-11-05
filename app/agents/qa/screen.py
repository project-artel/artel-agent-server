"""화면 판정 목록을 고치는 tool 둘이 모델에게 말하는 것, 그리고 그 답을 옮겨 적는 자리.

`app/agents/qa/knowledge.py` 와 같은 자리다 — tool 설명이 길고 그 설명이 사용 정책의 단일
출처라(ARTEL-192) `tools.py` 안에 두면 그 파일이 정책 문서가 된다.

## 이 tool 둘이 무엇을 고치는가

`screen` 하나는 그 `scene` 의 목록에 오른 selector 들의 on/off 조합이다. 목록에 없는
selector 는 화면 판정에 안 들어가고, 목록은 `capability.control_selector` 씨앗으로만 찬다 —
실측 빌드에서 capability 472개 중 24개만 그 값을 갖는다. 그래서 목록은 거의 언제나 얇고,
얇은 목록이 틀리는 방향은 **뭉치는 쪽**이다. 서로 다른 두 화면이 한 행에 앉고, 아무도 그
사실을 말해 주지 않는다.

## 왜 설명이 이렇게 긴가

이 두 tool 은 모델이 부를 일이 드물고, 부를 때는 지도를 영구히 바꾼다. 넣는 쪽이 헐거우면
그 `scene` 의 화면이 잘게 갈려 지도가 못 쓰게 되고, 그 손해는 이 런이 아니라 다음 런들이
문다. 그래서 설명이 지고 있는 것은 인자 사용법이 아니라 **선**이다 — 눈에 보이는 차이와
짐작을 가르는 선.
"""

from app.prompts import load_tool_description
from app.qa.envelope import ScreenSelectorResultPayload

# 목록에 앉힐 항목이 무엇을 가리키는가. orchestration 의 `ScreenSelectorMatch` 와 같은 셋이고,
# 셋뿐이다 — 넷째로 정규식을 두지 않는 이유는 이 항목이 Kotlin 과 SQL 양쪽에서 평가되기
# 때문이다(`docs/screen-selector-frames.md`).
SELECTOR_MATCH = "selector"
PATH_MATCH = "path"
SUBTREE_MATCH = "subtree"
SCREEN_SELECTOR_MATCHES = (SELECTOR_MATCH, PATH_MATCH, SUBTREE_MATCH)

# `scene_screen_selector.pattern` 의 길이. 저쪽 상한과 같은 값이고, 넘는 항목은 거절된다 —
# 왕복을 하나 아끼려고 여기서도 본다.
MAX_PATTERN_LENGTH = 512

def screen_tool_description(tool_name: str) -> str:
    """What the model reads for one screen selector tool, from `qa_run/<version>/tool_<tool_name>.md`."""
    return load_tool_description(tool_name).body


# 답이 안 온 경우에 붙이는 말. 지식 쓰기의 `UNCONFIRMED_WRITE` 와 같은 판단이다 — 침묵을
# 실패로 옮겨 적으면 모델이 같은 항목을 다시 보낸다.
UNCONFIRMED_RULE = (
    "No answer came back, so this side cannot say whether the list changed. It may "
    "well have. Do not send the same entry again — check the `content map:` line on "
    "your next observation instead."
)


def render_rule_result(payload: ScreenSelectorResultPayload) -> str:
    """받아들여진 것과 거절된 것을 모델이 읽는 문장으로.

    거절 사유를 그대로 옮긴다. 저쪽의 사유는 대부분 고칠 수 있는 것을 가리키고 — 이
    `scene` 에서 본 적 없는 경로, 셋 중 하나가 아닌 `match` — 요약하면 그 고칠 거리가
    사라진다.

    접힌 화면 수는 0 일 때도 말한다. **넣는 답이 0 인 것은 정상이고 그 사실 자체가
    가르침이다** — 그것을 안 말하면 모델은 과거 화면이 갈렸다고 믿은 채로 다음 스텝에
    간다.
    """
    lines: list[str] = []
    if payload.accepted:
        named = ", ".join(
            f"{entry.match} `{entry.pattern}` "
            f"({'tells screens apart' if entry.screen_defining else 'ignored'})"
            for entry in payload.accepted
        )
        lines.append(f"The content map took it: {named}.")
    else:
        lines.append("The content map stored nothing.")

    for entry in payload.rejected:
        target = f"{entry.match or '?'} `{entry.pattern or ''}`"
        lines.append(f"Refused — {target}: {entry.reason}")

    if payload.folded_screens > 0:
        lines.append(
            f"{payload.folded_screens} screen(s) in this scene became the same screen "
            "and were folded into one row."
        )
    elif payload.accepted and all(entry.screen_defining for entry in payload.accepted):
        # 0 이 정상인 쪽. 이 문장이 없으면 모델은 과거 화면이 갈렸다고 믿는다.
        lines.append(
            "No screens were folded, and that is what adding a selector does: the "
            "screens already recorded stay exactly as they are, and the split starts "
            "from your next observation."
        )
    elif payload.accepted:
        lines.append(
            "No screens were folded: dropping that selector left no two screens in "
            "this scene identical."
        )
    return "\n\n".join(lines)
