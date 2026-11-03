"""The verdict: judges a step after play stops, with the run's model.

It reads the step (what to do and the result to verify), why play stopped, the turns played —
the screen's scene, `done()` and what Jev said about it, the action picked, how its macro ended
and what it sent — and the values the planner named in the step's ```check block, read from
`STATE_SET` when play started, at the start of every turn and when it stopped (`StepPlay.evidence`).
The play's own record and the game's values are the evidence, with one picture of the screen when
play stopped if one could be taken (knob `screen_images`). It
answers `Verdict` — whether it passed, why in a sentence, the values that decided it, and an
insight (from prompt v4, `GameVerdict`'s `game_insight`: what the game showed in the step, apart from
the verdict) — through `structured`, the repo's one place that picks the structured-output method per
route. An answer that cannot be read is asked for once more; a second one fails the step with
`EVALUATOR-ERROR`.

Sending the verdict is `send.py`'s job, so a caller can judge without sending — a probe
script replaying a saved play does exactly that.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from app.agents.planner_executer.evidence import names_of, points_of, readings
from app.agents.planner_executer.executer import StepPlay
from app.agents.planner_executer.target import evaluator_note
from app.agents.planner_executer.prompts import (
    EVALUATOR_ROLE,
    LANGUAGE_PLACEHOLDER,
    SCENE_EVALUATOR_ROLE,
    case_view,
    prompt_text,
    this_step,
)
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.scenario import OutputLanguage
from app.llm.chat_model import structured
from app.qa.channel import QaCancelled

if TYPE_CHECKING:
    from app.agents.planner_executer.screen_capture import ScreenImage
from app.qa.run_config import RunConfig
from app.qa.schemas import QaScenario

# Tries per step: the first answer, and one more when it cannot be read.
ATTEMPTS = 2
# Turns shown; a long step keeps its last ones, where it stopped.
MAX_TURNS_SHOWN = 60
# Turns shown per case when a scene's cases are judged together; the request holds every case's.
MAX_TURNS_SHOWN_PER_CASE = 20
# Cases per scene judgement. A plan may verify up to `free_batch_size` cases; each case brings its
# turns and its end screen, so they are asked about a few at a time.
CASES_PER_JUDGEMENT = 5

# Names drawn from the ```check values, and the changes drawn per name. A long step keeps the first
# and the last changes, where it began and where it stopped.
MAX_EVIDENCE_NAMES = 80
MAX_EVIDENCE_CHANGES = 12
# A `flag(...)`'s observed values as one turn line shows them.
MAX_FLAG_CHARS = 300

_JSON = re.compile(r"\{.*\}", re.DOTALL)

# Sends the system and user text and returns the answer: a `Verdict`, a dict or a JSON string.
Judge = Callable[[str, str], Awaitable[Any]]


class Verdict(BaseModel):
    passed: bool
    # 판정 이유: 사람이 읽는 한두 문장.
    reason: str
    # 판정 상세 이유: 판정을 가른 값을 `STATE_SET` 기록 그대로 — 이름, 언제 무엇에서 무엇으로.
    state_detail: str
    # 이 step 에서 배운 것: 게임이나 계획, 근거에 대해 다음 step·런이 알아 둘 것. 없으면 빈 글이다.
    insight: str
    # 근거가 판정에 충분했나(묶음 3). 어떤 게임 값으로도 결과를 알 수 없어 실패로 둔 것이면 거짓이다 — 게임 결함과 관측 부족을
    # 가른다.
    evidence_sufficient: bool = True


class GameVerdict(BaseModel):
    """prompt v4 부터의 답. `insight` 대신 `game_insight` 를 쓴다 — 어떤 값이 더 나은 근거였는지는 `reason` 에 들어가고, 이 칸은
    이 step 에서 게임이 보인 모습에 대한 평가다(통과했어도 남은 문제). v3 까지의 런은 `Verdict` 로 그대로 묻는다."""

    passed: bool
    reason: str
    state_detail: str
    game_insight: str = ""
    evidence_sufficient: bool = True


@dataclass(frozen=True)
class StepVerdict:
    """`Evaluator.judge` 의 답. `message` 는 `reason` 에 멈춤 이유를 붙인 것이다."""

    passed: bool
    message: str
    state_detail: str = ""
    insight: str = ""
    evidence_sufficient: bool = True
    # evaluator 가 본 화면 한 장의 capture id. 그림 없이 판정했으면 `None` 이다.
    screen_capture_id: str | None = None
    # 이 step 에서 게임이 보인 모습에 대한 평가(prompt v4 부터). 옛 `insight` 와 섞지 않는다.
    game_insight: str = ""

    def details(self) -> dict[str, Any]:
        """step `STATUS` 의 `verdict`."""
        details: dict[str, Any] = {
            "state_detail": self.state_detail, "insight": self.insight, "evidence_sufficient": self.evidence_sufficient,
        }
        if self.screen_capture_id is not None:
            details["screen_capture_id"] = self.screen_capture_id
        if self.game_insight:
            details["game_insight"] = self.game_insight
        return details


def stop_note(stopped: str, language: OutputLanguage) -> str:
    """판정 이유 끝에 붙는 멈춤 사유. 이유가 run 의 언어로 쓰이므로 머리말도 맞춘다(PR #257 에서 가져옴). 사유 자체는
    Executer 의 진단이라 그대로 둔다."""
    if stopped == "done":
        return ""
    return f" (executer 중단: {stopped})" if language == OutputLanguage.ko else f" (executer stopped: {stopped})"


class Evaluator:
    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        # v4 부터 판정 이유를 run 의 언어로 쓴다. 그 전 버전은 placeholder 가 없어 이 값을 쓰지 않는다.
        self._system = prompt_text(
            EVALUATOR_ROLE, config.prompt_version, **{LANGUAGE_PLACEHOLDER: LANGUAGE_DIRECTIVES[config.language]}
        )
        self._judge = judge or self._ask_model
        # 답의 모양은 prompt 가 묻는 대로다. v4 부터 evaluator 글이 `game_insight` 를 묻는다.
        self._schema: type[Verdict] | type[GameVerdict] = GameVerdict if '"game_insight"' in self._system else Verdict

    async def judge(
        self,
        scenario: QaScenario,
        step: int,
        play: StepPlay,
        earlier: list[str] | None = None,
        image: "ScreenImage | None" = None,
    ) -> StepVerdict:
        """The step's verdict. The message names the stop when it was not `done`. `earlier` is the run's verdicts
        before this step, one line each — a result an earlier step reached is told apart from one that never came."""
        user = request(scenario, step, play, earlier)
        if image is not None:
            user += "\nA picture of the screen when play stopped is attached.\n"
        self._image = image
        problem = ""
        for _ in range(ATTEMPTS):
            try:
                verdict = _read(await self._judge(self._system, user), self._schema)
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
                continue
            note = stop_note(play.stopped, self._config.language)
            return StepVerdict(
                verdict.passed, verdict.reason + note, verdict.state_detail, getattr(verdict, "insight", ""),
                verdict.evidence_sufficient, image.capture_id if image is not None else None,
                game_insight=getattr(verdict, "game_insight", ""),
            )
        return StepVerdict(False, f"EVALUATOR-ERROR: no readable verdict in {ATTEMPTS} answers ({problem})")

    async def _ask_model(self, system: str, user: str) -> Verdict | GameVerdict:
        chain = structured(self._config.model, self._schema, self._config.reasoning)
        image = getattr(self, "_image", None)
        content = [{"type": "text", "text": user}, image.block()] if image is not None else user
        return await chain.ainvoke([SystemMessage(system), HumanMessage(content=content)])


class CaseVerdict(BaseModel):
    case_id: int
    passed: bool
    reason: str


class SceneVerdicts(BaseModel):
    verdicts: list[CaseVerdict]


class SceneEvaluator:
    """Judges every case played in one scene of a FREE scenario in one answer (ARTEL-1149)."""

    def __init__(self, config: RunConfig, judge: Judge | None = None) -> None:
        self._config = config
        self._system: str | None = None
        self._judge = judge or self._ask_model

    async def judge(
        self, scenario: QaScenario, played: list[tuple[int, StepPlay]], first_screen: str
    ) -> dict[int, tuple[bool, str]]:
        """`{step: (passed, message)}` for each played case, asked `CASES_PER_JUDGEMENT` at a time in
        play order. Each group starts from the screen the case before it left."""
        if self._system is None:
            self._system = prompt_text(SCENE_EVALUATOR_ROLE, self._config.prompt_version)
        results: dict[int, tuple[bool, str]] = {}
        start = first_screen
        for at in range(0, len(played), CASES_PER_JUDGEMENT):
            group = played[at : at + CASES_PER_JUDGEMENT]
            results.update(await self._judge_group(scenario, group, start))
            start = group[-1][1].end_screen
        return results

    async def _judge_group(
        self, scenario: QaScenario, played: list[tuple[int, StepPlay]], first_screen: str
    ) -> dict[int, tuple[bool, str]]:
        """One question for a few cases. A case the answers leave out is asked for once more with
        the rest; one still missing fails with `EVALUATOR-ERROR`."""
        by_case = {scenario.steps[step - 1].case_id: (step, play) for step, play in played}
        results: dict[int, tuple[bool, str]] = {}
        problem = ""
        for _ in range(ATTEMPTS):
            waiting = [(step, play) for step, play in played if step not in results]
            if not waiting:
                break
            try:
                verdicts = _read_scene(await self._judge(self._system, scene_request(scenario, waiting, first_screen)))
            except QaCancelled:
                raise
            except Exception as error:  # noqa: BLE001 - an unreadable answer is asked again
                problem = f"{type(error).__name__}: {str(error)[:200]}"
                continue
            for verdict in verdicts.verdicts:
                if verdict.case_id not in by_case:
                    continue
                step, play = by_case[verdict.case_id]
                note = stop_note(play.stopped, self._config.language)
                results[step] = (verdict.passed, verdict.reason + note)
            problem = "the answer left cases out"
        for step, _ in played:
            if step not in results:
                results[step] = (False, f"EVALUATOR-ERROR: no readable verdict for the case in {ATTEMPTS} answers ({problem})")
        return results

    async def _ask_model(self, system: str, user: str) -> SceneVerdicts:
        chain = structured(self._config.model, SceneVerdicts, self._config.reasoning)
        return await chain.ainvoke([SystemMessage(system), HumanMessage(user)])


def scene_request(scenario: QaScenario, played: list[tuple[int, StepPlay]], first_screen: str) -> str:
    """What the scene evaluator reads: the screen before the first case, then each case's play."""
    parts = [f"Screen before the first case began:\n{first_screen}\n"]
    for step, play in played:
        shown = play.turns[-MAX_TURNS_SHOWN_PER_CASE:]
        skipped = len(play.turns) - len(shown)
        turns = "\n".join(turn_line(turn) for turn in shown) or "  (no turn was played)"
        parts.append(
            f"Case:\n{json.dumps(case_view(scenario, step), ensure_ascii=False, indent=2)}\n\n"
            f"Why play stopped: {play.stopped}\n\n"
            f"Turns played ({len(play.turns)}"
            + (f", the first {skipped} left out" if skipped else "")
            + f"):\n{turns}\n\n"
            f"Screen when play stopped:\n{play.end_screen}\n"
        )
    return "\n---\n\n".join(parts)


def read_json(answer: Any) -> dict:
    """The JSON object in what the model returned — a dict as it is, a string by its first `{…}`."""
    if isinstance(answer, dict):
        return answer
    match = _JSON.search(str(answer))
    if match is None:
        raise ValueError(f"no JSON object in {str(answer)[:120]!r}")
    return json.loads(match.group(0))


def _read_scene(answer: Any) -> SceneVerdicts:
    """`SceneVerdicts` from what the model returned. Raises when it is not one."""
    if isinstance(answer, SceneVerdicts):
        return answer
    return SceneVerdicts.model_validate(read_json(answer))


def request(scenario: QaScenario, step: int, play: StepPlay, earlier: list[str] | None = None) -> str:
    """What the evaluator reads about one step."""
    shown = play.turns[-MAX_TURNS_SHOWN:]
    skipped = len(play.turns) - len(shown)
    turns = "\n".join(turn_line(turn) for turn in shown) or "  (no turn was played)"
    history = (
        "Earlier steps of this run and their verdicts:\n" + "\n".join(f"  {line}" for line in earlier) + "\n\n"
        if earlier
        else ""
    )
    return (
        history
        + f"The step:\n{this_step(scenario, step)}\n\n"
        + evaluator_note(getattr(play, "target", None))
        + f"Why play stopped: {play.stopped}\n\n"
        f"Turns played ({len(play.turns)}"
        + (f", the first {skipped} left out" if skipped else "")
        + f"):\n{turns}\n\n"
        f"{evidence_text(play)}\n"
    )


def evidence_text(play: StepPlay) -> str:
    """The active scene and the ```check values as the Evaluator's prompts draw them: each name once, with the
    readings where it changed. The first and the last reading are always shown, so a value that held still says so."""
    if play.state_source == "pulse_memory":
        return (
            "Game observations from PulseMemory (the SDK readings, not the executer's verdict):\n"
            f"Screen before play began:\n{play.start_screen or '(no reading available)'}\n\n"
            f"Screen when play stopped:\n{play.end_screen or '(no reading available)'}"
        )
    check = play.check
    named = []
    if check is not None and check.paths:
        named.append("paths " + ", ".join(check.paths))
    if check is not None and check.members:
        named.append("members " + ", ".join(check.members))
    asked = f"the plan named {'; '.join(named)}" if named else "the plan named no values (no ```check block)"
    head = (
        f"Game values ({asked}; the active scene is always read), read from the game state at the start, "
        "before each turn and at the end"
    )
    read = readings(play.evidence)
    failed = [f"{entry['at']}: {entry['error']}" for entry in play.evidence if "error" in entry]
    lines: list[str] = []
    if not read:
        lines.append("  (no reading succeeded)")
    names = names_of(read)
    for name in names[:MAX_EVIDENCE_NAMES]:
        lines.append(f"  {name}: {_series(name, read)}")
    if len(names) > MAX_EVIDENCE_NAMES:
        lines.append(f"  ({len(names) - MAX_EVIDENCE_NAMES} more names left out)")
    if play.evidence_unknown:
        lines.append(
            "  names the plan listed that matched nothing in any reading — a wrong name, or an object that never came; "
            "not by itself a sign the result is missing: " + ", ".join(play.evidence_unknown)
        )
    incomplete = [entry["at"] for entry in read if entry.get("clipped") or entry.get("omitted")]
    if incomplete:
        lines.append(
            "  readings that could not cover every object (a size limit or the server left some out): "
            + ", ".join(incomplete[:MAX_EVIDENCE_CHANGES])
        )
    if failed:
        lines.append("  readings that failed: " + "; ".join(failed[:MAX_EVIDENCE_CHANGES]))
    text = f"{head}:\n" + "\n".join(lines)
    if play.changes:
        more = f"\n  ({play.changes_left_out} more changes left out)" if play.changes_left_out else ""
        text += (
            "\nEverything else in the game that changed from the start to the end of play, whatever the plan named:\n"
            + "\n".join(f"  {line}" for line in play.changes)
            + more
        )
    return text


def _series(name: str, read: list[dict]) -> str:
    """`start 30 → turn 4: 22 → end 0`. A name missing from a complete reading is `(absent)`, from an incomplete
    one `(not read)` (`evidence.points_of`)."""
    points = points_of(name, read)
    last_at = read[-1]["at"]
    if len(points) == 1:
        return f"{points[0][1]} (unchanged from {read[0]['at']} to {last_at})"
    if len(points) > MAX_EVIDENCE_CHANGES:
        keep = MAX_EVIDENCE_CHANGES // 2
        dropped = len(points) - 2 * keep
        parts = [f"{at}: {value}" for at, value in points[:keep]] + [f"… {dropped} changes …"]
        parts += [f"{at}: {value}" for at, value in points[-keep:]]
    else:
        parts = [f"{at}: {value}" for at, value in points]
    if points[-1][0] != last_at:
        parts.append(f"{last_at}: {points[-1][1]}")
    return " → ".join(parts)


def turn_line(turn: dict) -> str:
    """One turn of a play as the Evaluator's prompts draw it."""
    plan = f"plan {turn['plan']} · " if turn.get("plan") else ""
    parts = [f"  {plan}turn {turn['turn']} on {turn.get('scene')}: done()={turn.get('done')}"]
    if turn.get("done_error"):
        parts.append(f"(done() could not be read: {turn['done_error']})")
    if "goal_ask" in turn:
        parts.append(f"Jev on the goal being reached: {turn['goal_ask']}")
    if "picked" in turn:
        parts.append(f"Jev picked {turn['picked']}")
    if "outcome" in turn:
        parts.append(f"→ {turn['outcome']}" + (f": {turn['reason']}" if turn.get("reason") else ""))
        sent = turn.get("sent") or []
        parts.append(f"sent {len(sent)} input{'s' if len(sent) != 1 else ''}" if sent else "sent no input")
        if turn.get("effect"):
            parts.append(f"its effect: {turn['effect']}")
        if turn.get("result"):
            parts.append(f"engine result: {turn['result']}")
    for flagged in turn.get("flags") or []:
        if isinstance(flagged, dict) and flagged.get("message"):
            observed = flagged.get("observed")
            parts.append(
                f"flag: {flagged['message']}"
                + (f" {json.dumps(observed, ensure_ascii=False, default=str)[:MAX_FLAG_CHARS]}" if observed else "")
            )
    if turn.get("repair"):
        parts.append(
            f"the editor rewrote the macros around {turn['repair']['action']}"
            if turn["repair"].get("replaced")
            else f"the editor's rewrite around {turn['repair']['action']} was refused"
        )
    if turn.get("jev_error"):
        parts.append(f"Jev did not answer: {turn['jev_error']}")
    return "; ".join(parts)


def _read(answer: Any, schema: type[Verdict] | type[GameVerdict] = Verdict) -> Verdict | GameVerdict:
    """The verdict in `schema`'s shape from what the model returned. Raises when it is not one."""
    if isinstance(answer, schema):
        return answer
    if isinstance(answer, BaseModel):
        return schema.model_validate(answer.model_dump())
    return schema.model_validate(read_json(answer))
