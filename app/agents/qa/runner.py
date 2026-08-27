"""The QA run as a tool loop.

The agent decides what to do next and reaches for a tool to do it. Nothing here
polls or waits on the game's initiative — that was the previous design's flaw:
a game that never volunteered its state left the run idle forever.
"""

import asyncio
import json
import logging

from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from app.agents.qa.arch import ResolvedArch
from app.agents.qa.compaction import QaCompactionMiddleware
from app.qa.scene import SCENE_VIEW_START_PREFIX
from app.agents.qa.context import (
    FOLDED_VIEW_PREFIX,
    fold_stale_knowledge,
    fold_stale_scenes,
)
from app.agents.qa.prompt import LANGUAGE_DIRECTIVES
from app.agents.qa.tools import QaRunState, build_tools
from app.agents.qa.vision import QaCaptureVisionMiddleware
from app.llm.chat_model import build_chat_model
from app.llm.models import LLMModel, get_model_spec
from app.prompts import load_prompt
from app.qa.channel import QaCancelled, QaRunChannel
from app.qa.envelope import LogCategory
from app.qa.schemas import QaScenario, QaStep
from app.qa.run_config import (
    COMPACTION_PROMPT_AGENT,
    COMPACTION_ROLE,
    PROMPT_AGENT,
    SYSTEM_ROLE,
    VISION_ROLE,
    RunConfig,
    resolve_run_config,
)

logger = logging.getLogger(__name__)

# A scene view runs long. Cut it for the console, and say it was cut — a silently
# truncated log reads as a smaller context than the model actually saw.
MAX_LOGGED_CHARS = 4000

# Content-block types that carry the model's own reasoning, and the keys those
# blocks put it under. Providers disagree on both.
_REASONING_BLOCK_TYPES = ("text", "thinking", "reasoning")
_REASONING_KEYS = ("text", "thinking", "reasoning", "reasoning_content")

# The graph nodes whose updates carry turns that actually happened. Everything
# else in an `astream` update is a middleware node reporting its own rewrite of
# the conversation, which is not new and must not be logged as if it were.
_TURN_PRODUCING_NODES = frozenset({"model", "tools"})


def _clip(text: str) -> str:
    if len(text) <= MAX_LOGGED_CHARS:
        return text
    return f"{text[:MAX_LOGGED_CHARS]}\n… [{len(text) - MAX_LOGGED_CHARS} more characters]"


def _action_line(step: QaStep) -> str:
    """스텝의 행위 한 줄. hint/input은 강제가 아닌 어드바이저리 근거로 덧붙인다."""
    line = step.action.strip() or "(no action)"
    extras: list[str] = []
    if step.hint:
        extras.append(f"try: {step.hint.strip()}")
    if step.input:
        extras.append(f"via: {step.input.strip()}")
    return f"{line}  ({'; '.join(extras)})" if extras else line


def _step_plan(scenario: QaScenario) -> list[tuple[int | None, bool]]:
    """스텝별 (case_id, is_verification). **연속 동일 case_id = 한 TC 구간**이고, 그 구간의
    **마지막 스텝 = 검증 스텝**(is_verification=True). case_id 없는 스텝은 검증이 아니다.

    report_step은 전체 스텝 수만큼 1..S로 오며, 이 표가 각 판정에 case_id·검증여부를 붙인다.
    TC 판정 = 그 구간 검증 스텝의 판정(파생)이라 여기 하드코딩되지 않는다.
    """
    steps = scenario.steps
    plan: list[tuple[int | None, bool]] = []
    for idx, step in enumerate(steps):
        cid = step.case_id
        # 구간의 마지막 스텝(다음 스텝의 case_id가 다르거나 끝)이면 검증 스텝.
        nxt = steps[idx + 1].case_id if idx + 1 < len(steps) else None
        is_verification = cid is not None and nxt != cid
        plan.append((cid, is_verification))
    return plan


def _plan(scenario: QaScenario) -> str:
    """새 계약(steps[])을 실행 첫 메시지로 변환한다(2단 판정 2026-08-08).

    **모든 스텝을 판정한다**: report_step(step, passed, message)를 전체 스텝(1..S)마다 호출한다.
    각 스텝은 `do`(행위)를 갖고, TC 구간의 첫 스텝엔 `precondition`, 마지막(검증) 스텝엔 `verify`
    (기대결과)가 붙는다. `verify`가 있는 스텝의 passed는 **기대결과가 나왔는가**(그게 그 TC의 최종
    판정), 그 외 스텝의 passed는 **그 행위를 수행/성립했는가**다.
    """
    plan = _step_plan(scenario)
    items: list[dict] = []
    prev_cid: int | None = None
    for idx, step in enumerate(scenario.steps):
        cid, is_verification = plan[idx]
        case = step.case
        item: dict = {"step": idx + 1, "do": _action_line(step)}
        if cid is not None:
            item["case_id"] = cid
            # 구간의 첫 스텝에만 사전조건을 건다(구간 진입 검증).
            if cid != prev_cid and case and case.precondition:
                item["precondition"] = case.precondition
            if is_verification:
                item["verify"] = case.expected if case else ""
        items.append(item)
        prev_cid = cid
    return (
        f"Scenario: {scenario.title} — {scenario.description}\n\n"
        "Execute the steps below in order and call report_step(step, passed, message) for EVERY step.\n"
        "- A step with `verify` is the TEST CASE's verdict: `passed` = whether that expected result "
        "actually occurred. That verdict is the whole case's verdict.\n"
        "- Any other step: `passed` = whether you carried the action out (did it happen), not a "
        "quality judgment.\n"
        "- A `precondition` must hold before that case's first step. If you cannot reach it, report "
        "its steps passed=false with a message beginning 'SETUP-FAILED:' and do NOT report_issue "
        "(an unreachable precondition is not a defect).\n"
        "- `try:` and `via:` are advisory hints, not required.\n\n"
        f"{json.dumps(items, ensure_ascii=False, indent=2)}\n\n"
        "Begin. Observe the screen first."
    )
def middleware_names_for(arch: ResolvedArch) -> tuple[str, ...]:
    """Which middleware this structure wraps its model calls in, in order.

    Names rather than objects, because the arch fingerprint has to be known
    before any run state exists — before there is a channel to build the real
    middleware against. `build_middleware` builds from this same list, so the two
    cannot drift into disagreeing about what a run actually did.

    Compaction is named first because it is the first thing that happens, but the
    position is documentation rather than wiring: it hooks `before_model`, its own
    graph node ahead of the model call, while everything after it wraps the call.
    Order only sequences middleware sharing a hook.
    """
    names = ["compaction"] if arch.compaction else []
    # Folding runs on every request; the scene views pile up whether or not the
    # model can see.
    if arch.fold_stale_scenes:
        names.append("fold_scene_views")
    # A separate middleware rather than one more job inside the fold above. The
    # two have to be switchable independently — they are experiment axes, and the
    # fingerprint hashes this list's order — and what they fold is recovered by
    # different tools out of different budgets.
    if arch.fold_stale_knowledge:
        names.append("fold_knowledge_neighbours")
    # Only when the run can see. On a text-only run it would have nothing to
    # inject and nothing to trim.
    if arch.vision:
        names.append("capture_vision")
    # 여기에 라이브 씬을 붙이던 자리다. 뺐다 — 매 모델 호출 맨 뒤에 붙었다 사라지는 메시지가
    # 프롬프트 접두를 매 턴 깨뜨려, 캐시가 시스템 프롬프트에서 멈춰 있었다. 크기의 문제가
    # 아니었고 10 토큰짜리 고정 꼬리도 똑같이 깼다.
    #
    # 화면은 이제 도구 결과가 낸다. `create_agent` 는 도구 호출이 없는 응답에 루프를 끝내므로
    # (`langchain/agents/factory.py`) 런 중간의 모든 모델 호출은 직전 도구 결과 뒤에 온다 —
    # 꼬리가 메우려던 틈이 애초에 없었다(ARTEL-621).
    #
    # 사용량 로깅은 가장 안쪽이다. 실제로 나간 것을 보고해야 한다.
    names += ["log_token_usage"]
    return tuple(names)


def build_middleware(
    arch: ResolvedArch,
    state: QaRunState,
    channel: QaRunChannel,
    config: RunConfig,
    on_compacted=None,
) -> list:
    """The middleware named by `middleware_names_for`, in that order."""
    builders = {
        "compaction": lambda: QaCompactionMiddleware(
            # Not the run's model, and not `cache_prompt`: summarizing is one
            # call over a prefix nothing will send again, so caching it would pay
            # the write premium for a read that never happens.
            model=build_chat_model(LLMModel(config.compaction_model)),
            summary_prompt=load_prompt(
                COMPACTION_PROMPT_AGENT, COMPACTION_ROLE, config.compaction_prompt_version
            ).body,
            run_model_max_input_tokens=get_model_spec(config.model).max_input_tokens,
            state=state,
            channel=channel,
            trigger_fraction=arch.compaction_trigger_fraction,
            keep_messages=arch.compaction_keep_messages,
            min_new_messages=arch.compaction_min_new_messages,
            trim_tokens=arch.compaction_trim_tokens,
            on_compacted=on_compacted,
        ),
        "fold_scene_views": lambda: _fold_scene_views,
        "fold_knowledge_neighbours": lambda: _fold_knowledge_neighbours,
        "capture_vision": lambda: QaCaptureVisionMiddleware(state),
        "log_token_usage": lambda: _log_token_usage,
    }
    return [builders[name]() for name in middleware_names_for(arch)]


@wrap_model_call
async def _fold_scene_views(request, handler):
    """Fold stale scene views out of what one model call actually receives.

    `request.override` replaces only this call's messages, not the graph's own
    state, so the timeline and the console logging below keep the full text —
    see `app/agents/qa/context.py` for the fold itself.
    """
    return await handler(request.override(messages=fold_stale_scenes(request.messages)))


@wrap_model_call
async def _fold_knowledge_neighbours(request, handler):
    """Fold volunteered neighbour blocks out of what one model call receives.

    Model-input only, like the scene fold above. Only the neighbour lines go —
    the hits themselves stay, because re-reading one costs a search and the run
    only has six. See `app/agents/qa/context.py`.
    """
    return await handler(request.override(messages=fold_stale_knowledge(request.messages)))


def _context_shape(messages) -> str:
    """무엇이 컨텍스트를 채웠는지, 한 줄로.

    총량만으로는 무엇을 고쳐야 하는지가 안 나온다. 실제로 stage 런이 137k 에서 163k 까지
    한 번도 안 꺾이고 자랐는데, 그 턴당 1,000 이 어디서 오는지 로그가 말하지 않아 추정으로
    최적화하게 됐다 — 판독 렌더를 줄이는 데 매달렸지만 라이브 뷰는 `request.override` 로 그
    호출에만 붙고 누적되지 않으므로 애초에 주범이 아니었다(ARTEL-604).

    캐시가 조용히 실패하듯 컨텍스트도 조용히 찬다. 그래서 조사용이 아니라 상시로 둔다.

    라이브 뷰를 따로 세던 칸이 있었다. 그 답이 나왔으므로 뺐다 — 매 턴 교체되던 그 꼬리가
    프롬프트 접두를 깨뜨리는 원인이었고, 지금은 화면이 도구 결과 안에 있다(ARTEL-621).

    세는 자리가 요점이다. 이 미들웨어는 순서상 마지막이라 여기 오는 `messages` 는 접기와
    라이브 뷰가 모두 적용된 뒤 — **실제로 나가는 그 목록**이다.

    토큰은 근사로 센다. 정확한 값은 provider 가 `usage_metadata` 로 주고, 이 줄이 답하는
    것은 총량이 아니라 **비율**이다.
    """
    folded = kept = 0
    by_kind: dict[str, int] = {}

    for message in messages:
        size = count_tokens_approximately([message])
        # `content` 를 직접 읽는다. `.text` 는 langchain 버전에 따라 메서드였다 프로퍼티였다
        # 하고, 그 차이로 조용히 빈 문자열이 되면 분해가 전부 0 으로 보인다.
        content = message.content
        text = content if isinstance(content, str) else ""

        if isinstance(message, ToolMessage):
            # 접힌 자리와 전문으로 남은 자리를 가른다. `fold_stale_scenes` 가 실제로
            # 얼마나 누르는지는 이 둘의 비에서만 나온다.
            if FOLDED_VIEW_PREFIX in text:
                folded += 1
            elif SCENE_VIEW_START_PREFIX in text:
                kept += 1

        kind = type(message).__name__.removesuffix("Message").lower()
        by_kind[kind] = by_kind.get(kind, 0) + size

    total = sum(by_kind.values())
    if total <= 0:
        return "messages=0"

    parts = [
        f"{kind}={size}({100 * size // total}%)"
        for kind, size in sorted(by_kind.items(), key=lambda pair: -pair[1])
    ]
    parts.append(f"views folded={folded} kept={kept}")
    return f"approx={total} " + " ".join(parts)


@wrap_model_call
async def _log_token_usage(request, handler):
    """Log what each model call cost, how much came from cache, and what filled it.

    Prompt caching fails silently: a prefix under the model's minimum size, or one
    that changed since the last call, is billed in full with no error and no
    warning. `cache_read` in the details is the only signal that the
    `cache_control` set in `app/llm/chat_model.py` is doing anything, so a run's
    log has to carry it or the setting is unverifiable in production.

    The details dict is logged whole rather than picked apart: which keys
    OpenRouter fills in varies by provider, and an unread key is worth more here
    than a `0` for one this provider never sends.

    The shape line comes from the request rather than the response, so it is
    written even when a call fails before any usage comes back — a call that died
    on input size is exactly the one whose shape someone will want.
    """
    # 계측이 런을 죽이지 않는다. 여기서 오르는 예외는 모델 호출을 통째로 날리는데,
    # 그것을 감수할 만한 것이 로그 한 줄에는 없다.
    messages = getattr(request, "messages", None)
    if messages is not None:
        logger.info("[QA] context %s", _context_shape(messages))

    response = await handler(request)
    for message in response.result:
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        logger.info(
            "[QA] tokens input=%d output=%d details=%s",
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            usage.get("input_token_details") or {},
        )
    return response


class QaRunner:
    """Runs one scenario to completion over one channel."""

    def __init__(self, config: RunConfig | None = None) -> None:
        # Resolved elsewhere and only read here — see `app/qa/run_config.py` for
        # why nothing is decided at run time any more. The default exists so a
        # test can build a runner without restating every axis.
        self._config = config if config is not None else resolve_run_config()

    @property
    def config(self) -> RunConfig:
        return self._config

    def _tool_call_limit(self, steps: int) -> int:
        return self._config.arch.tool_call_limit(steps)

    async def _log_compaction(self, summary: str, count: int) -> None:
        """The summary on the console, whole.

        The timeline says that a compaction happened; only the log can say what
        the model was left with, and that is the first thing anyone reads when a
        run goes strange right after one.
        """
        logger.info("[QA] context compacted #%d\n%s", count, _clip(summary))

    async def run(
        self, channel: QaRunChannel, scenario: QaScenario, state: QaRunState
    ) -> None:
        config = self._config
        arch = config.arch
        # Loaded by the version resolved at open, so both halves come from one
        # version and the text is the text `config.prompt_hashes` recorded.
        prompt = load_prompt(PROMPT_AGENT, SYSTEM_ROLE, config.prompt_version)
        # A separate file in the same version: it is prompt text, tuned by whoever
        # tunes the rest of it, and only reaches runs that can see. Telling a
        # text-only model about a tool it does not have would send it looking for
        # one.
        vision_directive = (
            load_prompt(PROMPT_AGENT, VISION_ROLE, config.prompt_version).body
            if arch.vision
            else ""
        )
        system_prompt = prompt.body.format(
            language_directive=LANGUAGE_DIRECTIVES[config.language],
            vision_directive=vision_directive,
        )
        first_message = _plan(scenario)
        total_steps = len(scenario.steps)
        tools = build_tools(channel, state, arch)

        # The whole starting context in one place. Reading a run afterwards means
        # knowing what the model was actually given, and the prompt is assembled
        # from several pieces that are otherwise only visible in source. The
        # resolved config goes out whole rather than as picked-out fields: an axis
        # added later reaches the log by being in the config, not by someone
        # remembering to add it here too.
        logger.info(
            "[QA] run starting\n"
            "  config=%s steps=%d\n"
            "--- system prompt ---\n%s\n"
            "--- first message ---\n%s",
            config.model_dump(mode="json", exclude_none=True),
            total_steps,
            system_prompt,
            _clip(first_message),
        )

        # Membership and order live in `middleware_names_for`, so the list the
        # run actually uses and the list the fingerprint is computed from are the
        # same list. Building them apart is how a structure changes without its
        # identity moving.
        middleware = build_middleware(
            arch, state, channel, config, on_compacted=self._log_compaction
        )

        agent = create_agent(
            # Prompt caching is asked for here and nowhere else: the agent loop
            # resends the whole conversation every turn, which is the one shape
            # that reads back more than it writes. See `build_chat_model`.
            model=build_chat_model(config.model, config.reasoning, cache_prompt=True),
            tools=tools,
            system_prompt=system_prompt,
            middleware=middleware,
        )
        # Streamed rather than invoked so the model's reasoning can be put on the
        # timeline as it happens. Left to a tool the agent chooses to call, the
        # reasoning simply never appears — it has no reason to narrate itself, and
        # a run that only shows actions gives no way to tell a considered decision
        # from a lucky one.
        async for update in agent.astream(
            {"messages": [("user", first_message)]},
            # recursion_limit counts graph steps, so it bounds tool calls as well
            # as the model turns between them.
            {
                # Two graph steps per iteration — the model call and the tool
                # call — plus one for each middleware that runs as its own node
                # ahead of the model. Left at a flat 2, turning compaction on
                # would quietly cost the run a third of its tool budget.
                "recursion_limit": (
                    self._tool_call_limit(total_steps)
                    * (2 + (1 if arch.compaction else 0))
                ),
                "run_name": "qa-scenario-run",
                "tags": ["agent", "qa"],
                # What a trace has to be groupable by. The prompt version and the
                # arch identity are here for the same reason they are recorded at
                # all: a trace that only says which model ran cannot separate a
                # prompt change from a structural one.
                "metadata": {
                    "qa_try_id": channel.qa_try_id,
                    "model": config.model.value,
                    "language": config.language.value,
                    "prompt_version": config.prompt_version,
                    "agent_arch": config.agent_arch,
                    "agent_fingerprint": config.agent_fingerprint,
                    "git_sha": config.git_sha,
                },
            },
            stream_mode="updates",
        ):
            await self._log_reasoning(channel, update)

    @staticmethod
    def _text_of(message) -> str:
        """The assistant's own words, without the tool-call scaffolding.

        Reasoning is not always a `text` block. Anthropic emits `thinking`,
        several models fronted by OpenRouter emit `reasoning`, and some carry it
        beside the content in `additional_kwargs` rather than inside it. Reading
        only `text` is why a whole run reached the timeline with no reasoning on
        it at all.
        """
        parts: list[str] = []

        content = getattr(message, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") not in _REASONING_BLOCK_TYPES:
                    continue
                # The payload key is usually the block's own type, but `text` is
                # used for reasoning blocks too. Take whichever is present.
                for key in _REASONING_KEYS:
                    value = block.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
                        break

        extra = getattr(message, "additional_kwargs", None) or {}
        for key in _REASONING_KEYS:
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
                break

        seen: list[str] = []
        for part in parts:
            stripped = part.strip()
            # A provider that sends reasoning both ways would otherwise log twice.
            if stripped and stripped not in seen:
                seen.append(stripped)
        return "\n".join(seen)

    async def _log_reasoning(self, channel: QaRunChannel, update: dict) -> None:
        """Put each model turn on the timeline, and the whole exchange on the console.

        The timeline carries what a reviewer needs; the console carries what a
        developer needs — including the tool results, which are most of what the
        model is actually reading and are invisible everywhere else.
        """
        for name, node in update.items():
            # Only the two nodes that produce new turns. A middleware node —
            # compaction is one — reports its whole rewritten message list as its
            # update, so every preserved AIMessage in it would be logged and put
            # on the timeline a second time. The operator would see the agent's
            # reasoning repeat itself after each compaction, with no clue why.
            if name not in _TURN_PRODUCING_NODES:
                continue
            if not isinstance(node, dict):
                continue
            for message in node.get("messages", []) or []:
                kind = getattr(message, "type", None)

                if kind == "tool":
                    content = _clip(str(getattr(message, "content", "")))
                    logger.info(
                        "[QA] tool result ← %s\n%s",
                        getattr(message, "name", "?"),
                        content,
                    )
                    await self._log_tool_result(channel, message, content)
                    continue

                if kind != "ai":
                    continue

                text = self._text_of(message)
                calls = getattr(message, "tool_calls", None) or []
                logger.info(
                    "[QA] model turn\n  text: %s\n  calls: %s",
                    _clip(text) if text else "(none)",
                    [{"name": call.get("name"), "args": call.get("args")} for call in calls]
                    or "(none)",
                )
                # 모델이 제 말로 쓴 추론. tool 의 `thought` 인자와 다른 것이라 남긴다 —
                # 저쪽은 호출 하나를 설명하는 한 줄이고, 이쪽은 무엇을 부를지 고른 이유다.
                if text:
                    await channel.note(text, LogCategory.THOUGHT)
                for call in calls:
                    await self._log_tool_call(channel, call)

    @staticmethod
    def _step_of(args: dict) -> int | None:
        """그 호출이 붙은 시나리오 스텝. 거의 모든 tool 이 `step` 을 인자로 받는다.

        모델이 채우는 값이라 믿고 쓰지 않는다. 정수가 아니면 없는 것으로 둔다 — 화면이
        이것으로 로그를 스텝 구간에 나누므로, 엉뚱한 값이 들어가면 한 줄이 남의 구간에
        가서 앉는다.
        """
        step = args.get("step")
        return step if isinstance(step, int) and not isinstance(step, bool) else None

    async def _log_tool_call(self, channel: QaRunChannel, call: dict) -> None:
        """호출 하나를 타임라인에 낸다.

        이름과 id 가 없는 호출은 내지 않는다. 그런 것은 짝지을 수도, 무엇이 불렸는지 말할
        수도 없어서 화면에 이름 없는 줄만 하나 늘린다.
        """
        tool = call.get("name")
        tool_call_id = call.get("id")
        if not tool or not tool_call_id:
            return
        # LangChain 이 `tool_calls[].args` 를 dict 로 검증하므로 모양은 믿어도 된다.
        # 없는 경우만 있다 — 인자 없는 tool 이 그렇다.
        args = call.get("args") or {}
        await channel.tool_call(tool, tool_call_id, args, self._step_of(args))

    async def _log_tool_result(self, channel: QaRunChannel, message, content: str) -> None:
        """그 호출의 답을 타임라인에 낸다.

        `content` 는 부르는 쪽이 이미 자른 것을 받는다. 콘솔과 타임라인이 같은 문자열을
        보게 하려는 것이고, 자르는 길이가 두 자리에서 갈리지 않게 하려는 것이다.

        스텝은 답에 실려 오지 않는다. 화면은 짝이 되는 호출 프레임에서 그것을 읽는다.
        """
        tool_call_id = getattr(message, "tool_call_id", None)
        if not tool_call_id:
            return
        await channel.tool_result(
            str(getattr(message, "name", "") or "tool"), tool_call_id, content
        )

    async def run_with_deadline(
        self, channel: QaRunChannel, scenario: QaScenario
    ) -> tuple[QaRunState, str | None]:
        """Returns the state and, when the run did not close cleanly, why.

        The state is built here and handed down so that a run cut short by the
        deadline still carries the verdicts it managed to record.
        """
        step_meta = _step_plan(scenario)
        state = QaRunState(total_steps=len(scenario.steps), step_meta=step_meta)
        deadline = self._config.arch.deadline_seconds
        try:
            await asyncio.wait_for(self.run(channel, scenario, state), timeout=deadline)
        except asyncio.TimeoutError:
            return state, f"The run exceeded its {int(deadline)}s limit."
        except QaCancelled:
            return state, None
        except Exception as error:  # noqa: BLE001 - the reason has to reach the timeline
            return state, f"The run stopped on an error: {error}"
        if not state.finished:
            return state, "The agent stopped without closing the run."
        return state, None
