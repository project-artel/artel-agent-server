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

# 두 설명이 공유하는 부분. 인자의 뜻은 두 tool 에서 글자 그대로 같고, 같은 것을 두 번 적으면
# 언젠가 한쪽만 고쳐진다.
_HOW_TO_NAME_IT = """`pattern` is an EXACT string, never a regular expression. There is no wildcard
here: it is compared literally, character for character, so `.*` matches no
selector at all and comes back refused. The pattern is also checked against what
this scene has actually been seen holding, so a typo is refused and told to you
rather than stored as an entry that silently matches nothing. Copy the string out
of the pulse view in front of you.

`match` says what the pattern is:

- `selector` — one exact selector, sibling indices and all, as the pulse view
  prints it: `CombineSystem[7]/CombineZone[1]/Zone1[0]`. Use it when that exact
  object is the thing that differs.
- `path` — the same selector with every sibling index stripped:
  `CombineSystem/CombineZone/Zone1`. Use it when the indices move between
  observations, which they do whenever the game spawns and destroys things, and
  the object is the same object each time.
- `subtree` — that path and everything below it, matched at node boundaries.
  Use it only when the whole branch appears and disappears as one.

`reason` is what you saw, in one sentence, written for someone who was not here.
It is required. An entry nobody can retrace is an entry nobody can ever decide to
remove, and this list is meant to be maintained rather than accumulated.

The scene is the one you are standing on right now. You do not name it and you
cannot reach another scene's list from here — a scene you are not standing on is
one you have not observed, so you have no grounds about it."""

INCLUDE_SCREEN_SELECTOR_DESCRIPTION = f"""Tell the content map that this selector is one of the things that tells screens apart in this scene.

Call this when the game is plainly showing you a DIFFERENT screen — a panel
opened over the board, a menu replaced what was there, a result overlay came up —
and the `content map:` line in your scene view still names the same screen id it
named before. That mismatch means this scene's list is missing the selector that
would have told the two apart, and you are standing in the only place from which
it can be seen.

Do not call it on a hunch. Every selector added is one more axis along which this
scene's screens can split, and a map split into dozens of near-identical screens
is worse than one that merged two — nobody can read it and nothing can be built
on it. The bar is a difference you can SEE: something appeared, disappeared, or
changed at the same moment the map stayed put. A selector that merely looks
important, or that you think might matter later, does not clear it.

**This does not un-merge the screens that already merged.** Those rows were
written without this selector's value in them, so there is nothing to restore and
the map cannot go back and split them — that value is gone for good. What you
change here takes effect from your NEXT observation onward, and from the first
observation of every run after this one. So make the call once, when you see the
mismatch, and carry on with the step: calling it again will not recover the past,
and the screen you are on now will keep the id it has.

{_HOW_TO_NAME_IT}"""

EXCLUDE_SCREEN_SELECTOR_DESCRIPTION = f"""Tell the content map that this selector does NOT tell screens apart in this scene.

Call this when the map has split one screen into several that are the same screen
to a player: the `content map:` line keeps naming a new screen id while the game
in front of you has not changed in any way you could describe to someone else.
Look at what the line says it is telling screens apart by — a counter, a timer, a
spawned object, anything whose value moves on its own — and name that one here.

This is the direction that repairs the past. Excluding a selector rewrites the
screens this scene has already recorded, folds the ones that become identical
onto a single row, and the result tells you how many disappeared. That is worth
doing when the scene is genuinely over-split, and worth being careful about for
the same reason: the folded rows do not come back, and a later entry putting the
selector back cannot restore the value the fold erased.

The bar is the mirror of the other tool's. Do not exclude a selector because it
looks noisy — exclude it because you have watched the screen NOT change while
the map said it did.

{_HOW_TO_NAME_IT}"""

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
