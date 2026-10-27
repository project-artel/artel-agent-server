"""developer macro 의 `selector` 가 게임이 실제로 보여 주는 object 를 겨누는지, QA 런의 `pulse` 로 본다.

정적 검사는 경로가 build 에 있는지 모른다. 그래서 prefab 에만 있고 실행 중에는 거의 켜지지 않는 object 를
겨눈 macro 가 통과했다. WordVenture build 4 의 `dismiss_dialogue` 는 `TutorialController/ChatWindow/ChatText`
로 튜토리얼 대사를 찾았는데, 최근 QA 런 10개의 `pulse` 에서 그 경로는 live 로 0번 나왔고 실제로 글자를 그리는
`TutorialController/ChatWindow/DialogueWindow/DialoguePanel/ChatText` 는 2406번 나왔다. macro 는 대사가 없다고
보고 아무것도 누르지 않았고, 튜토리얼의 입력 막이 전투 내내 남았다(로컬 try 194).

`observed` 는 orchestration 이 그 build 의 최근 QA 런 `PULSE` 에서 센, 경로(`[n]` 없는 `path`) → live 로 실린
reading 수다. 비어 있으면 아무것도 하지 않는다 — 런이 없는 build 에는 근거가 없다.

거절하는 것은 "같은 이름으로 끝나고 같은 root 아래인 다른 경로가 훨씬 자주 보였는데 이 경로는 거의 안 보였다"
는 경우뿐이다. 한 번도 안 보인 경로라도 그런 대안이 없으면 통과시킨다. 아직 아무 런도 가지 않은 화면일 수 있다.
"""

import ast
import re
from collections.abc import Mapping

from app.agents.qa.macro.errors import MacroRejection

# 대안이 이 배수보다 많이 보였으면 이 경로는 "거의 안 보인" 것이다.
RARE_RATIO = 20
# 대안이 적어도 이만큼은 보였어야 근거로 친다. 몇 번 안 되는 숫자끼리의 비는 우연이다.
MIN_ALTERNATIVE_READINGS = 20
# 거절 문장에 적는 대안 수.
MAX_SUGGESTIONS = 3

_INDEX = re.compile(r"\[\d+\]")


def _bare(path: str) -> str:
    return _INDEX.sub("", path).strip().strip("/")


def _seen(path: str, observed: Mapping[str, int]) -> tuple[str, int]:
    """`(observed 의 경로, 횟수)`. 맨 앞 마디가 scene 이름이면 떼고도 찾는다."""
    if path in observed:
        return path, observed[path]
    head, _, rest = path.partition("/")
    if rest and rest in observed:
        return rest, observed[rest]
    return path, 0


def _alternatives(path: str, count: int, observed: Mapping[str, int]) -> list[tuple[str, int]]:
    parts = path.split("/")
    leaf, root = parts[-1], parts[0]
    found = [
        (other, seen)
        for other, seen in observed.items()
        if other != path
        and other.split("/")[-1] == leaf
        and other.split("/")[0] == root
        and seen >= MIN_ALTERNATIVE_READINGS
        and count * RARE_RATIO < seen
    ]
    return sorted(found, key=lambda pair: -pair[1])[:MAX_SUGGESTIONS]


def reject_rarely_shown_selectors(source: str, observed: Mapping[str, int] | None) -> None:
    """거의 안 보이는 object 를 겨누는 `selector("...")` 리터럴이 있으면 줄 번호와 함께 `MacroRejection`."""
    if not observed:
        return
    try:
        module = ast.parse(source)
    except SyntaxError:
        return
    for node in ast.walk(module):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "selector"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        written = node.args[0].value
        path, count = _seen(_bare(written), observed)
        better = _alternatives(path, count, observed)
        if not better:
            continue
        shown = ", ".join(f"`{other}` ({seen})" for other, seen in better)
        raise MacroRejection(
            f"`selector({written!r})` is refused: in recent QA runs on this build the game reported "
            f"`{path}` live in {count} readings, while {shown} with the same name under the same root "
            "showed up far more. The object the game actually shows is probably one of those; the path "
            "in a prefab or scene file is not always where the game draws it at run time. Aim at the "
            "one that is shown.",
            node.lineno,
        )
