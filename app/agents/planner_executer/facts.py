"""개발자가 선언한 게임 fact·rule. planner 가 step 마다 쓸 fact 를 고르고, Jev 는 그 값을 `facts` 로 읽는다.

fact 는 게임 값 하나에 플레이어 말의 이름과 뜻을 붙인 것이다. `source` 는 렌더러가 내놓는 이름 그대로다 — 객체 member 는
`Type.member` 짧은 이름(중첩 경로는 점으로 이어진다, 목록 크기는 `<목록>.Count`), static 은 `statics` 키 이름. 이름을 추측해
맞추지 않는다. 틀린 이름은 읽히지 않고 `unknown` 으로 보인다.

rule 은 값과 행동의 관계를 말하는 게임 지식이다. planner 만 읽는다. 행동 결정 코드로 바꾸지 않는다.

PoC 단계라 선언은 로컬 파일(`qa_facts_dir` 의 `facts.json`·`rules.json`)에서 읽는다. MCP 등록은 별도 작업이다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.agents.qa.macro.errors import MacroRejection

if TYPE_CHECKING:
    from app.qa.channel import QaRunChannel

logger = logging.getLogger(__name__)

FACTS_FILE = "facts.json"
RULES_FILE = "rules.json"
# fact 이름은 선언과 guidance 에서 그대로 쓰인다.
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TYPES = ("integer", "number", "boolean", "string")
# 읽지 못한 fact 의 값. 0·false 로 바꾸지 않는다.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class GameFact:
    name: str
    scope: Literal["objects", "statics"]
    member: str
    type: str
    means: str
    unit: str | None = None

    def definition(self) -> str:
        return self.means + (f" ({self.unit})" if self.unit else "")


@dataclass(frozen=True)
class GameRule:
    name: str
    says: str
    facts: tuple[str, ...]


@dataclass(frozen=True)
class GameFacts:
    facts: tuple[GameFact, ...]
    rules: tuple[GameRule, ...]
    # 선언 두 파일의 내용 hash. 런 기록에 남겨 어떤 선언으로 돌았는지 가린다.
    revision: str

    def get(self, name: str) -> GameFact | None:
        return next((fact for fact in self.facts if fact.name == name), None)


NO_FACTS = GameFacts((), (), "")


def load_game_facts(directory: str | None) -> GameFacts:
    """`directory` 의 선언. 없거나 읽지 못하면 `NO_FACTS` 이고 log 를 남긴다. 규칙에 어긋난 항목은 빼고 log 를 남긴다."""
    if not directory:
        return NO_FACTS
    root = Path(directory)
    try:
        facts_raw = (root / FACTS_FILE).read_bytes()
        rules_path = root / RULES_FILE
        rules_raw = rules_path.read_bytes() if rules_path.exists() else b"[]"
        facts_json, rules_json = json.loads(facts_raw), json.loads(rules_raw)
    except (OSError, ValueError) as error:
        logger.warning("[QA] game facts not loaded from %s: %s", directory, error)
        return NO_FACTS
    facts: list[GameFact] = []
    for item in facts_json:
        problem = _fact_problem(item, {fact.name for fact in facts})
        if problem:
            logger.warning("[QA] game fact skipped: %s", problem)
            continue
        source = item["source"]
        facts.append(GameFact(item["name"], source["scope"], source["member"], item["type"], item["means"], item.get("unit")))
    names = {fact.name for fact in facts}
    rules: list[GameRule] = []
    for item in rules_json:
        refs = tuple(item.get("facts") or ())
        missing = [ref for ref in refs if ref not in names]
        if not item.get("name") or not item.get("says") or not refs or missing:
            logger.warning("[QA] game rule skipped: %s (facts not declared: %s)", item.get("name"), missing)
            continue
        rules.append(GameRule(item["name"], item["says"], refs))
    revision = hashlib.sha256(facts_raw + b"\0" + rules_raw).hexdigest()[:12]
    return GameFacts(tuple(facts), tuple(rules), revision)


def _fact_problem(item: dict, taken: set[str]) -> str | None:
    name = item.get("name")
    source = item.get("source") or {}
    if not isinstance(name, str) or not _NAME.match(name):
        return f"{name!r}: the name must be snake_case, at most 40 characters"
    if name in taken:
        return f"{name}: declared twice"
    if source.get("scope") not in ("objects", "statics") or not source.get("member"):
        return f"{name}: the source needs a scope (objects or statics) and a member"
    if item.get("type") not in _TYPES:
        return f"{name}: the type must be one of {', '.join(_TYPES)}"
    if not item.get("means"):
        return f"{name}: the fact says nothing about what it means"
    return None


def planner_text(declared: GameFacts) -> str:
    """planner 요청에 붙는 선언. source 이름은 싣지 않는다 — planner 가 경로를 다시 쓰지 않게 런타임이 맞춘다."""
    if not declared.facts:
        return ""
    lines = [
        "Game facts (declared by the game's developer). Name the facts this step's play depends on in a ```facts block, "
        "one name per line. For those facts only, the decision model reads each turn's value at the top of its state, "
        f"under `facts`; a value the game did not report that turn reads \"{UNKNOWN}\". In guidance, write every "
        "condition that depends on one of them with its name and value (`when facts.<name> is 0`), not in your own words.",
    ]
    lines += [f"- {fact.name} ({fact.type}): {fact.definition()}" for fact in declared.facts]
    if declared.rules:
        lines.append("")
        lines.append("Game rules (how the game works, declared by the developer; each names the facts it is about):")
        lines += [f"- {rule.name} [{', '.join(rule.facts)}]: {rule.says}" for rule in declared.rules]
    return "\n".join(lines) + "\n\n"


def read_facts_block(text: str | None, declared: GameFacts) -> tuple[str, ...]:
    """```facts 블록의 이름들, 블록이 없으면 빈 묶음, 선언에 없는 이름이 있으면 `MacroRejection`."""
    if text is None:
        return ()
    names = tuple(dict.fromkeys(line.strip().strip("-").strip() for line in text.splitlines() if line.strip()))
    unknown = [name for name in names if declared.get(name) is None]
    if unknown:
        raise MacroRejection(
            f"the ```facts block names {', '.join(unknown)}, which is not a declared fact; use only the names in the "
            "Game facts list, one per line."
        )
    return names


_GUIDANCE_FACT = re.compile(r"\bfacts\.([a-z][a-z0-9_]*)")


def check_guidance_facts(guidance: str | None, chosen: tuple[str, ...], declared: GameFacts) -> None:
    """guidance 가 `facts.<이름>` 으로 부른 fact 가 ```facts 블록에 다 있는지. 없으면 `MacroRejection`.

    ```facts 에 없는 fact 는 Jev 에게 가지 않는다. guidance 가 Jev 가 못 보는 값을 조건으로 쓰지 않게 계획 단계에서 막는다.
    """
    named = list(dict.fromkeys(_GUIDANCE_FACT.findall(guidance or "")))
    undeclared = [name for name in named if declared.get(name) is None]
    unlisted = [name for name in named if declared.get(name) is not None and name not in chosen]
    if undeclared:
        raise MacroRejection(
            f"the guidance names {', '.join('facts.' + name for name in undeclared)}, which is not a declared fact; "
            "use only the names in the Game facts list."
        )
    if unlisted:
        raise MacroRejection(
            f"the guidance names {', '.join('facts.' + name for name in unlisted)} but the ```facts block does not list "
            "it, so the decision model would not see its value; list in the ```facts block every fact the guidance names."
        )

WATCH_TIMEOUT_SECONDS = 10.0


def watch_name(fact: GameFact) -> str:
    """`watch` 가 받는 꼴. 선언의 첫 점을 `::` 로 바꾼다 — `CardManager.myCards.Count` → `CardManager::myCards.Count`."""
    declaring, _, path = fact.member.partition(".")
    return f"{declaring}::{path}"


async def watch_facts(channel: "QaRunChannel", declared: GameFacts) -> list[dict]:
    """선언한 fact 의 source 를 SDK 에 `watch` 로 한 번 청한다. 그 뒤로 선언한 경로 그대로 매 pulse 에 실린다. 못 푼 것을 돌려준다.

    이름을 고쳐 다시 청하지 않는다. 못 푼 fact 는 런 동안 "unknown" 으로 보인다.
    """
    from app.agents.planner_executer.decisions import note_decision
    from app.qa.envelope import JsonRpcAction

    names = [watch_name(fact) for fact in declared.facts]
    if not names:
        return []
    result = await channel.dispatch_actions(
        [JsonRpcAction(id=1, method="watch", params=[names])], message="watch declared facts", timeout=WATCH_TIMEOUT_SECONDS
    )
    item = result.results[0] if result is not None and result.results else None
    if item is None or not item.success:
        reason = "no answer from the SDK" if item is None else (item.error or "the SDK refused watch")
        missing = [{"name": name, "reason": reason} for name in names]
        returned: dict = {}
    else:
        returned = dict(getattr(item, "returnValue", None) or {})
        missing = [dict(entry) for entry in returned.get("unresolvable") or []]
    await note_decision(
        channel, "facts_watch", f"watch: {len(names) - len(missing)}/{len(names)} declared fact sources resolved",
        names=names, resolved=returned.get("resolved") or [], unresolvable=missing,
    )
    return missing
