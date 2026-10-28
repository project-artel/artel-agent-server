"""The editor: rewrites one step's macro source while the executer plays it.

The planner writes a step before it is played, from the screen it sees then, and nothing says
whether an action will move the game until the game answers. A macro cannot be checked before it
is published either: its effect is the game's answer, which only a run has. So the check happens
in play. When one action keeps failing, or keeps running without sending the game an input
(`Executer`, `repair_after`), the executer calls this instead of ending the step, and goes on with
the source this returns.

It is a role of its own rather than a second call to the planner, so that the planner's prompt
stays about writing a step and this one about fixing it from evidence. The evidence is the turn
records of the action — the runner's failure reason or the fact that nothing was sent — and the
screen now. The answer is a whole source of the shape the planner writes, read with the same
parser (`parse_step`); a refused answer goes back with the parser's sentences, up to
`max_rewrites` times, like the planner's. Developer macros selected for the step stay as they
are: they are registered macros the editor cannot change, and when the failing action is one of
them it is taken out of the step and the editor is told to write a `def` in its place.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.planner_executer.arch import PlannerExecuterArchSpec
from app.agents.planner_executer.decisions import (
    MAX_QUOTE_CHARS,
    MAX_REFUSAL_CHARS,
    MAX_SOURCE_CHARS,
    clip,
    note_decision,
)
from app.agents.planner_executer.developer_macros import DEVELOPER_ACTION_PREFIX
from app.agents.planner_executer.macros import DeveloperAction, RegisteredMacros, StepMacros, parse_step
from app.agents.planner_executer.planner import Ask, _text, extract_code
from app.agents.planner_executer.prompts import MACRO_REPAIR_ROLE, game_map, prompt_text, screen, this_step
from app.agents.qa.macro.errors import MacroRejection
from app.llm.chat_model import build_chat_model
from app.qa.channel import QaRunChannel
from app.qa.envelope import LogCategory
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# `kind` of the decision logs this writes: the rewrite that replaced the step's macros, and a refusal.
MACRO_FIX = "macro_fix"
MACRO_FIX_REFUSED = "macro_fix_refused"
# How many of the action's turns the request shows. The executer passes the streak that called it.
MAX_EVIDENCE_TURNS = 5
MAX_EVIDENCE_REASON_CHARS = 400

# What the executer calls: `(macros as they run now, the action, its turn records)` → the rewrite,
# or `None` when it was refused to the end.
Repair = Callable[[StepMacros, str, list[dict]], Awaitable[StepMacros | None]]


@dataclass(frozen=True)
class Repaired:
    """The editor's last answer: parsed, or the refusal it still gets."""

    source: str
    macros: StepMacros | None
    refusal: str | None = None


class MacroEditor:
    def __init__(
        self,
        config: RunConfig,
        channel: QaRunChannel,
        spec: PlannerExecuterArchSpec,
        registered: RegisteredMacros,
        ask: Ask | None = None,
    ) -> None:
        self._config = config
        self._channel = channel
        self._spec = spec
        self._registered = registered
        self._system = prompt_text(MACRO_REPAIR_ROLE, config.prompt_version)
        self._ask = ask or self._ask_model
        self._model = None

    def for_step(self, scenario: QaScenario, step: int) -> Repair:
        """The callable the executer takes for one step."""

        async def repair(macros: StepMacros, action: str, turns: list[dict]) -> StepMacros | None:
            return (await self.repair(scenario, step, macros, action, turns)).macros

        return repair

    async def repair(
        self, scenario: QaScenario, step: int, macros: StepMacros, action: str, turns: list[dict]
    ) -> Repaired:
        """Step `step`'s source rewritten around `action`; rewritten again while the parser refuses
        it, up to `max_rewrites`. `turns` are the action's turn records that called the editor."""
        developer_actions = {
            key: DeveloperAction(definition, macros.descriptions.get(key, ""))
            for key, definition in macros.actions.items()
            if key.startswith(DEVELOPER_ACTION_PREFIX) and key != action
        }
        feedback = ""
        repaired = Repaired(macros.source, None, "nothing was written")
        for attempt in range(self._spec.max_rewrites + 1):
            await self._channel.note(
                f"Step {step}: the editor is rewriting the macros around `{action}`"
                + (" again (the last answer was refused)" if attempt else "")
                + f". Prompt {self._config.prompt_version}.",
                LogCategory.THOUGHT,
                step,
            )
            answer = await self._ask(self._system, self._request(scenario, step, macros, action, turns, feedback))
            source = extract_code(answer)
            try:
                repaired = Repaired(source, await parse_step(source, self._registered, developer_actions))
            except MacroRejection as rejection:
                repaired = Repaired(source, None, rejection.render())
                await note_decision(
                    self._channel,
                    MACRO_FIX_REFUSED,
                    f"Step {step}: the rewrite around `{action}` was refused (answer {attempt + 1} of "
                    f"{self._spec.max_rewrites + 1}): {clip(repaired.refusal, MAX_QUOTE_CHARS)}",
                    step,
                    action=action,
                    answer=attempt + 1,
                    refusal=clip(repaired.refusal, MAX_REFUSAL_CHARS),
                    source=clip(source, MAX_SOURCE_CHARS),
                )
                feedback = (
                    "Your previous answer cannot be used as written. The macro parser refused it:\n"
                    f"  {repaired.refusal}\n"
                    "The refusal names what is accepted instead. Rewrite the whole source."
                )
                continue
            before, after = list(macros.actions), list(repaired.macros.actions)
            await note_decision(
                self._channel,
                MACRO_FIX,
                f"Step {step}: the macros were rewritten around `{action}` — {len(after)} actions: {', '.join(after)}",
                step,
                action=action,
                evidence=evidence_lines(turns),
                actions_before=before,
                actions=after,
                source=clip(source, MAX_SOURCE_CHARS),
            )
            break
        return repaired

    def _request(
        self, scenario: QaScenario, step: int, macros: StepMacros, action: str, turns: list[dict], feedback: str
    ) -> str:
        drawn = game_map(self._channel.scene.scene_context)
        shown = turns[-MAX_EVIDENCE_TURNS:]
        developer = action.startswith(DEVELOPER_ACTION_PREFIX)
        return (
            (f"Game map:\n{drawn}\n\n" if drawn else "")
            + f"Scenario: {scenario.title}\n\n"
            + f"The step:\n{this_step(scenario, step)}\n\n"
            + f"The step's macros as they run now:\n```python\n{macros.source.rstrip()}\n```\n\n"
            + f"The action that does not progress the step: `{action}`"
            + (
                " — a developer macro selected for the step, not in the source above; it is taken out of the "
                "step, so write a `def` that does its work."
                if developer
                else ""
            )
            + f"\nIts docstring: {macros.description(action)!r}\n"
            + f"How its last {len(shown)} runs went:\n"
            + "\n".join(evidence_lines(shown))
            + f"\n\nCurrent screen:\n{screen(self._channel.scene.pulse)}\n"
            + (f"\n{feedback}\n" if feedback else "")
        )

    async def _ask_model(self, system: str, user: str) -> str:
        if self._model is None:
            self._model = build_chat_model(self._config.model, self._config.reasoning)
        answer = await self._model.ainvoke([SystemMessage(system), HumanMessage(user)])
        return _text(answer.content)


def evidence_lines(turns: list[dict]) -> list[str]:
    """One line per turn record: the scene, the outcome, and what was sent or why it stopped."""
    lines = []
    for turn in turns:
        line = f"  turn {turn.get('turn')} on {turn.get('scene')}: {turn.get('outcome')}"
        if turn.get("reason"):
            line += f" — {str(turn['reason'])[:MAX_EVIDENCE_REASON_CHARS]}"
        sent = turn.get("sent") or []
        if sent:
            line += "; sent " + ", ".join(str(batch.get("summary")) for batch in sent)
        else:
            line += "; no input was sent to the game"
        lines.append(line)
    return lines
