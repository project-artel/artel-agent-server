"""무엇이 지금 `screen` 이라고 판정됐는지, QA agent 가 볼 수 있게.

`screen` 하나는 그 `scene` 의 목록에 오른 selector 들의 on/off 조합이다(`discriminator`,
ARTEL-654). 목록 밖 selector 는 무시되므로 목록이 얇으면 서로 다른 두 화면이 한 행에
앉는다. **그리고 그렇게 뭉치는 것은 조용하다** — 어디에도 경고가 뜨지 않는다.

그것을 알아챌 수 있는 자리는 게임을 하고 있는 QA agent 하나뿐이다. 화면이 눈에 띄게
바뀌었는데 지도가 같은 화면이라고 하면 그 불일치를 본 것은 그 agent 다. 이 모듈은 그
agent 가 그 불일치를 **볼 수 있게** 하는 절반이고, 나머지 절반인 고치는 tool 은
`app/agents/qa/tools.py` 에 있다.

## 어디서 오는가

프레임 둘이 같은 값을 싣는다.

- `SCREEN_SETTLED` — 관측이 화면을 확정했고 그것이 직전과 다를 때마다(ARTEL-668).
  **여기가 이 블록이 기대는 통로다.**
- `SCREEN_SELECTOR_PROPOSAL` — "이 selector 가 화면을 가르는가" 를 물어보면서 곁들여
  싣는다(ARTEL-655). 답하는 것은 따로 띄우는 판정 agent 다(ARTEL-656)

둘째만 있던 시절이 `SCREEN_SETTLED` 가 생긴 이유다. 제안은 `(scene, selector)` 마다 평생
한 번만 나가므로 이미 한 번 플레이한 빌드에서는 한 장도 안 오고, 그때 이 블록은 런 내내
비어 있었다 — 목록을 고치는 tool 둘이 부를 계기를 잃는 상태다.

## 왜 그래도 "지금 읽은 값" 이 아닌가

`SCREEN_SETTLED` 도 화면이 **바뀔 때** 나가지 관측마다 나가지 않는다. 그래서 이 블록이
말하는 것은 지금 화면의 실황이 아니라 지도가 그 `scene` 에 대해 마지막으로 한 말이다.

두 가지로 막는다. 판정을 그 판정이 가리키는 `scene` 이름과 함께 들고 있다가 agent 가 그
`scene` 에 서 있을 때만 그리고, 그릴 때 그것이 **지도가 마지막으로 한 말**이지 지금 읽은
값이 아니라고 블록 자신이 말한다. 다른 `scene` 의 판정을 지금 화면인 척 그리는 것이 이
블록이 낼 수 있는 최악의 오류라, 그 경우는 아예 안 그린다.
"""

from pydantic import BaseModel, Field

from app.qa.envelope import (
    ScreenDiscriminatorEntry,
    ScreenSelectorProposalPayload,
)

# 한 블록에 이름 댈 `discriminator` 항목 수.
#
# 이 블록은 도구 결과마다 실리고 대화에 남는다. 목록이 잘게 갈린 `scene` 에서는 항목이
# 수십이 될 수 있는데, 그때 필요한 것은 명단 전부가 아니라 "무엇으로 가르고 있나" 의
# 감각이다. 잘린 것은 잘렸다고 말한다 — 조용히 줄인 목록은 그것이 전부인 것으로 읽힌다.
MAX_DISCRIMINATOR_SHOWN = 12


class ScreenVerdict(BaseModel):
    """지도가 마지막으로 말한 `screen` 하나.

    `scene` 을 함께 든다. 이것이 이 모델의 요점이다 — 판정은 화면이 바뀔 때만 오므로
    agent 가 다른 `scene` 으로 넘어간 뒤에도 값이 남아 있고, `scene` 을 안 들면 그 값을
    지금 화면으로 그리게 된다.
    """

    scene: str
    screen_id: str
    name: str | None = None
    discriminator: list[ScreenDiscriminatorEntry] = Field(default_factory=list)
    # 이 화면 직전에 굳었던 화면. 런의 첫 화면이거나 저쪽이 재시작했으면 없다.
    previous_screen_id: str | None = None


class ScreenMap(BaseModel):
    """지도가 이 런에 대해 마지막으로 한 말.

    판정 하나만 든다. 이력을 쌓지 않는 것은 이 블록이 답하는 질문이 "지금 어느 화면이라고
    하나" 하나이기 때문이고, 이력이 필요한 판단은 여기가 아니라 화면을 접는 저쪽에서
    난다.
    """

    verdict: ScreenVerdict | None = None

    def apply(self, payload: ScreenSelectorProposalPayload) -> None:
        """프레임 하나에 실려 온 화면 판정을 받아 둔다.

        `SCREEN_SETTLED` 와 `SCREEN_SELECTOR_PROPOSAL` 이 같은 세 필드를 같은 철자로
        싣는다. 그래서 이 메서드가 둘 다 받고, 어느 쪽에서 왔는지는 여기서 안 본다 —
        받아 두는 값이 글자 하나 다르지 않기 때문이다.

        `current_screen` 이 없으면 아무것도 안 바꾼다. 제안에서는 아직 어떤 화면도 굳지
        않은 시점의 정상적인 모양이고, 그때 판정을 비우면 직전에 받아 둔 멀쩡한 값이
        사라진다.
        """
        current = payload.current_screen
        scene = payload.scene.name.strip()
        if current is None or not scene or not current.screen_id:
            return
        self.verdict = ScreenVerdict(
            scene=scene,
            screen_id=current.screen_id,
            name=current.name,
            discriminator=list(current.discriminator),
            previous_screen_id=(
                payload.previous_screen.screen_id if payload.previous_screen else None
            ),
        )

    def render(self, scene_name: str | None) -> str | None:
        """`scene_name` 에 서 있는 agent 에게 보여 줄 블록, 없으면 `None`.

        **다른 `scene` 의 판정은 안 그린다.** 이름을 정확히 맞대는 것도 같은 이유다 —
        게임은 `Battle` 과 `Battle 2` 를 둘 다 가질 수 있고, 느슨하게 맞추면 옆 `scene` 의
        화면 번호를 지금 화면으로 내놓는다.

        같은 판정을 두 번 그려도 같은 글이 나온다. 화면이 바뀌었다는 사실은 "지난번에
        보여 준 것과 다르다" 가 아니라 판정 자신이 싣고 온 `previous_screen` 에서
        나온다 — 압축 원장과 도구 결과가 같은 판정을 각각 그리므로, 그린 자리를 장부로
        들면 둘이 서로의 소식을 먹는다.
        """
        verdict = self.verdict
        if verdict is None or not scene_name or verdict.scene != scene_name:
            return None

        where = f"screen {verdict.screen_id}"
        if verdict.name:
            where = f"{where} ({verdict.name})"
        head = f"content map: you are on {where} of {verdict.scene}"
        if verdict.previous_screen_id:
            head = f"{head}, reached from screen {verdict.previous_screen_id}"

        return "\n".join([head, f"  {_told_apart_by(verdict)}", f"  {_CAVEAT}"])


# 블록이 스스로 말해야 하는 것 둘. 이것이 지금 읽은 값이 아니라는 것, 그리고 틀렸을 때
# 무엇으로 고치는가.
#
# **언제 고쳐야 하는지는 여기 없다.** 그것은 tool 설명의 몫이다(ARTEL-192) — 정책을 두
# 자리에 적으면 한쪽만 고쳐지고, 그때 모델이 읽는 것은 둘 중 어느 쪽인지 아무도 모른다.
_CAVEAT = (
    "(the map's last word on this scene, not a live reading. "
    "`include_screen_selector` and `exclude_screen_selector` change what it tells "
    "screens apart by)"
)


def _told_apart_by(verdict: ScreenVerdict) -> str:
    """이 화면을 다른 화면과 가른 selector 들, 있는 그대로.

    빈 `discriminator` 를 "없음" 으로 흘리지 않는다. 그것은 이 `scene` 의 목록에 오른
    selector 가 여기서 하나도 안 나타났다는 뜻이고, 그 `scene` 의 모든 관측이 이 한 행에
    앉아 있다는 뜻이다 — 이 tool 들이 존재하는 이유가 정확히 그 상태다.
    """
    if not verdict.discriminator:
        return (
            "told apart by: nothing. No selector on this scene's list showed up here, "
            "so every observation in this scene lands on this one screen row."
        )

    shown = verdict.discriminator[:MAX_DISCRIMINATOR_SHOWN]
    named = ", ".join(
        f"{entry.selector} {'on' if entry.active else 'off'}" for entry in shown
    )
    cut = len(verdict.discriminator) - len(shown)
    if cut > 0:
        named = f"{named} (+{cut} more not listed)"
    return f"told apart by: {named}"
