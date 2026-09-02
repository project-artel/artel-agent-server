"""Catching a flow that asks a value to climb without ever moving it (ARTEL-648).

A scenario can require the same value at rising levels — `StagePosition >= 1`, then
`>= 2`, then `>= 3` — and never pass through the thing that raises it. Run it and the
second check never comes true; whoever is playing stalls there.

**The model reads the rule and still does this.** The case list says, under the very
requirement, that the value only moves on one screen and that there is no button for
it. Prompt v11 says to put the steps that move it in between. Measured (run 208), the
authored result still had eight of these. Grouping is a judgement about one case at a
time and the model does it well; ordering asks it to hold forty-two items at once,
and that is the part that slips.

So the turn checks its own work and asks again — for the one scenario that is wrong,
with only that scenario's cases in front of it. Measured (A/B on the same model), the
same job on one journey at a time took unreachable climbs from nine to one.

## What counts as wrong

Only a climb: the same value required **higher** later in the same flow, with nothing
in between that moves it. A flow that opens at `>= 4` and stays there is not caught —
that is a starting condition, and orchestration says so in the first step.

The value's own screen is not a climb either. `position` rises with an arrow key on
the very screen the case runs on, so a flow that walks 0 → 1 → 2 needs nothing between.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.scenario.schemas import AuthoredStep, ScenarioPlan, TestCaseListItem

# Comparisons that name a floor. `!= 5` and `<= 2` do not say "at least this much",
# so a later one of those is not a climb.
_CLIMBING = ("==", ">=", ">")


@dataclass(frozen=True)
class UnreachableClimb:
    """One place a flow asks for more than it has made.

    `where` is the screen the map says moves the value. It is what the rewritten
    scenario has to pass through, so it belongs in the message the model gets back.
    """

    case_id: int
    variable: str
    had: float
    wants: float
    where: tuple[str, ...]

    def describe(self) -> str:
        where = " · ".join(self.where) if self.where else "어디서 오르는지 명세에 없음"
        return (
            f"case {self.case_id} 이 {self.variable} 을 {self.had:g} 에서 {self.wants:g} 로"
            f" 요구하는데, 그 사이에 그 값을 올리는 것이 없습니다 ({where} 에서 오릅니다)"
        )


def _level(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def climbs_in(steps: list[AuthoredStep], cases: dict[int, TestCaseListItem]) -> list[UnreachableClimb]:
    """Walk one flow top to bottom and report every climb nothing paid for."""
    highest: dict[str, float] = {}
    moved_since: dict[str, bool] = {}
    # Where each value moves, remembered as the walk meets it. A step that passes
    # through one of those screens pays for the value — and the step that does the
    # passing usually carries no guard of its own (`Return` into the battle), so this
    # cannot be read off the case being looked at.
    movers: dict[str, set[str]] = {}
    found: list[UnreachableClimb] = []

    for step in steps:
        case = cases.get(step.case_id) if step.case_id is not None else None
        if case is None:
            continue
        for guard in case.state_before:
            wants = _level(guard.value)
            if wants is None or guard.operator not in _CLIMBING:
                continue
            # Where the map says this value moves, other than right here. A value that
            # moves on this very screen is reachable by playing this screen.
            elsewhere = tuple(
                sorted({m.scene for m in guard.moves if m.scene != case.scene})
            ) or tuple(sorted(s for s in guard.raised_in if s != case.scene))
            if not elsewhere:
                continue
            had = highest.get(guard.variable)
            if had is not None and wants > had and not moved_since.get(guard.variable):
                found.append(
                    UnreachableClimb(
                        case_id=case.id,
                        variable=guard.variable,
                        had=had,
                        wants=wants,
                        where=elsewhere,
                    )
                )
            if had is None or wants > had:
                highest[guard.variable] = wants
                moved_since[guard.variable] = False
        for guard in case.state_before:
            movers.setdefault(guard.variable, set()).update(
                {m.scene for m in guard.moves} | set(guard.raised_in)
            )
        # Running this case leaves the flow on the screen it arrives at, and passing
        # through a screen is how these values move.
        stood_on = {case.scene, case.state_after.get("scene")}
        for variable, where in movers.items():
            if where & stood_on:
                moved_since[variable] = True

    return found


def unreachable_climbs(
    scenarios: list[ScenarioPlan], test_case_list: list[TestCaseListItem]
) -> dict[int, list[UnreachableClimb]]:
    """Which authored scenarios ask for a climb they never make, by their index."""
    cases = {case.id: case for case in test_case_list}
    if not cases:
        return {}
    found = {}
    for index, scenario in enumerate(scenarios):
        climbs = climbs_in(scenario.steps, cases)
        if climbs:
            found[index] = climbs
    return found
