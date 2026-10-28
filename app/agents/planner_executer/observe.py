"""The case's `observe` names, sent to the SDK as one `watch` before the run plays (ARTEL-1279).

A person writing a test case names the value it checks — `Player::hp`, `Progress::stage` — and the
SDK resolves the name against the running game and adds it to the watch list, so the value rides
every `pulse` from then on. The names the SDK could not resolve come back in the same answer and
are kept per case: a failure on such a case is `INCONCLUSIVE`, not a defect the model gets to
explain (`app/agents/evaluator/analysis.py:unobservable`).

One batch for the whole scenario. The SDK walks the game's references once per batch, and a
batch per case would walk it once per case.
"""

from typing import Any

from app.agents.planner_executer.decisions import note_decision
from app.qa.channel import QaRunChannel
from app.qa.envelope import JsonRpcAction
from app.qa.schemas import QaScenario

WATCH_TIMEOUT_SECONDS = 10.0


def observe_names(scenario: QaScenario) -> list[str]:
    """Every case's `observe` names, in step order, each once."""
    names: list[str] = []
    for step in scenario.steps:
        for name in step.case.observe if step.case and step.case.observe else []:
            if name not in names:
                names.append(name)
    return names


async def watch(
    channel: QaRunChannel, scenario: QaScenario
) -> dict[int, list[dict[str, Any]]]:
    """Send the scenario's `observe` names as one `watch`; the unresolvable ones, by case id.

    No names, no action. An SDK that does not know `watch` refuses the action, and that is filed
    the same way as every name unresolvable — the cases are then `INCONCLUSIVE` when they fail,
    which is what a build whose pulse cannot carry the value deserves.
    """
    names = observe_names(scenario)
    if not names:
        return {}
    result = await channel.dispatch_actions(
        [JsonRpcAction(id=1, method="watch", params=[names])],
        message="watch observe names",
        timeout=WATCH_TIMEOUT_SECONDS,
    )
    item = result.results[0] if result is not None and result.results else None
    if item is None or not item.success:
        reason = (
            "no answer from the SDK"
            if item is None
            else (item.error or "the SDK refused watch")
        )
        missing = [{"name": name, "reason": reason} for name in names]
        returned: dict[str, Any] = {}
    else:
        returned = dict(getattr(item, "returnValue", None) or {})
        missing = [dict(entry) for entry in returned.get("unresolvable") or []]
    await note_decision(
        channel,
        "watch",
        f"watch: {len(names) - len(missing)}/{len(names)} observe names resolved",
        names=names,
        resolved=returned.get("resolved") or [],
        unresolvable=missing,
        walk=returned.get("walk"),
    )
    unresolved = {entry.get("name") for entry in missing}
    by_case: dict[int, list[dict[str, Any]]] = {}
    for step in scenario.steps:
        if step.case is None or not step.case.observe:
            continue
        lost = [
            entry
            for entry in missing
            if entry.get("name") in set(step.case.observe) & unresolved
        ]
        if lost:
            by_case[step.case.id] = lost
    return by_case
