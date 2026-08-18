"""arm 셋 — 같은 근거를 세 형태로 조립한다.

프롬프트는 arm 마다 쓰지 않고 틀 하나에 근거 블록만 갈아 끼운다. arm 별로 문장을 따로
쓰면 나중에 나온 차이가 입력 형식 때문인지 문장 때문인지 가를 수 없다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

PROMPTS = Path(__file__).parent / "prompts"


class Arm(StrEnum):
    #: content_map JSON 만. capabilityId·status·selector·verification 이 있다.
    content_map = "a"
    #: 의사 C# 렌더만. `reached from:` 처럼 조인 결과를 문장으로 들고 있지만 id 가 없다.
    pseudo_cs = "b"
    #: 둘 다.
    both = "c"


_EVIDENCE_NOTE = {
    Arm.content_map: (
        "형식은 content_map JSON 이다. 씬마다 기능 행이 있고, 행마다 `capabilityId`,"
        " 전제(`given`), 조작(`when`), 결과(`then`)가 실려 있다."
    ),
    Arm.pseudo_cs: (
        "형식은 근거에서 렌더한 의사 C# 소스다. 컴파일되지 않는다 — 본문에는 실제로 관측된"
        " 문장만 IL 오프셋 순서로 들어 있고, 주석 `// reached from:` 과 `// called by:` 는"
        " 어디서 그 메서드에 닿는지를 말한다."
    ),
    Arm.both: (
        "두 형식이 함께 온다. 앞은 content_map JSON(씬별 기능 행, `capabilityId` 포함),"
        " 뒤는 같은 근거에서 렌더한 의사 C# 소스다. 둘은 같은 캡처에서 나왔다."
    ),
}

_CITATION_NOTE = {
    Arm.content_map: (
        "`capabilityId` 로 인용하라. `unit` 은 null 로 둔다."
    ),
    Arm.pseudo_cs: (
        "`unit` 으로 인용하라 — `네임스페이스.타입.메서드` 꼴이다"
        " (예: `Tutorial.TutorialController.Start`). `capabilityId` 는 null 로 둔다."
    ),
    Arm.both: (
        "`capabilityId` 로 인용하고, 그 사실이 의사 C# 쪽에서 온 것이면 `unit` 도 함께 적어라."
    ),
}


@dataclass(frozen=True)
class ArmInput:
    arm: Arm
    system: str
    human: str
    evidence_sha256: str

    @property
    def approximate_tokens(self) -> int:
        return len(self.human) // 3


def read_pseudo_cs(directory: Path) -> str:
    """렌더된 의사 C# 을 파일명 헤더와 함께 하나로 잇는다.

    `wv2cs.py` 를 여기서 부르지 않는다 — 렌더러도 캡처도 이 레포 밖 입력이고,
    측정이 렌더러 버전에 따라 달라지면 안 된다. 이미 렌더된 것을 받는다.
    """
    files = sorted(path for path in directory.rglob("*.cs"))
    if not files:
        raise FileNotFoundError(f"no .cs files under {directory}")
    return "\n\n".join(
        f"// ===== {path.relative_to(directory)} =====\n{path.read_text(encoding='utf-8')}"
        for path in files
    )


def build_arm_input(arm: Arm, content_map_text: str, pseudo_cs_text: str) -> ArmInput:
    if arm is Arm.content_map:
        evidence = content_map_text
    elif arm is Arm.pseudo_cs:
        evidence = pseudo_cs_text
    else:
        evidence = f"# content_map\n\n{content_map_text}\n\n# 의사 C#\n\n{pseudo_cs_text}"

    human = (PROMPTS / "human.md").read_text(encoding="utf-8").format(
        evidence_note=_EVIDENCE_NOTE[arm],
        citation_note=_CITATION_NOTE[arm],
        evidence=evidence,
    )
    return ArmInput(
        arm=arm,
        system=(PROMPTS / "system.md").read_text(encoding="utf-8"),
        human=human,
        evidence_sha256=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
    )
