"""What a planner–executer run was executed with, in the same `RunConfig` the tool loop records.

Orchestration stores one shape for every run and groups by `agent_arch` and `agent_fingerprint`,
so this engine fills the same fields with its own label and digest. The tool loop's knobs still
ride along in `arch`; `arch.engine` says they were not what ran.
"""

from app.agents.planner_executer.arch import planner_executer_fingerprint
from app.agents.planner_executer.prompts import FAILURE_ANALYSIS_ROLE, PROMPT_AGENT, RUN_REVIEW_ROLE, static_pieces
from app.agents.qa.arch import QaArchError, ResolvedArch
from app.agents.qa.macro.grammar import TOOL_NAMES
from app.agents.scenario import OutputLanguage
from app.config import get_settings
from app.llm.chat_model import TEMPERATURE
from app.llm.decision import resolve_decision_model
from app.llm.models import LLMModel, ReasoningConfig, get_model_spec
from app.prompts import roles_in
from app.qa.run_config import RunConfig


def resolve_planner_executer_config(
    *,
    model: LLMModel,
    language: OutputLanguage,
    prompt_version: str | None,
    reasoning: ReasoningConfig | None,
    arch: ResolvedArch,
) -> RunConfig:
    """Settle a planner–executer run. `model` is the planner's model; `reasoning` is validated.

    `prompt_version` names a `qa_planner` version, resolved here so the alias never reaches the
    record; the hashes cover the planner's rules, the evaluator's prompt and the macro grammar.
    The planner and the evaluator both run on `model`.
    """
    assert arch.planner is not None, "resolve_arch fills `planner` for this engine"
    version, hashes = static_pieces(prompt_version)
    # 실패 분석과 런 리뷰는 v4 부터 있는 글을 읽는다. 옛 version 에 `analysis="on"` 을 두면 run 은
    # 첫 실패에서 `PromptError` 로 죽는다. 조용히 끄면 `analysis="on"` 으로 기록된 run 이 분석 없이
    # 돌아 다른 구조 아래 쌓인다 — `resolve_arch` 가 `vision='on'` 을 거절하는 것과 같은 이유로 거절한다.
    roles = roles_in(PROMPT_AGENT, version)
    if arch.planner.analysis == "on" and not {FAILURE_ANALYSIS_ROLE, RUN_REVIEW_ROLE} <= set(roles):
        raise QaArchError(
            f"planner.analysis='on' needs the `{FAILURE_ANALYSIS_ROLE}` and `{RUN_REVIEW_ROLE}` prompts, "
            f"and qa_planner/{version} has neither. Pin v4 or newer, or send planner.analysis='off'."
        )
    functions = step_module_functions()
    settings = get_settings()
    return RunConfig(
        model=model,
        provider=get_model_spec(model).provider.value,
        temperature=TEMPERATURE,
        reasoning=reasoning,
        reasoning_supported=get_model_spec(model).reasoning is not None,
        language=language,
        prompt_version=version,
        prompt_hashes=hashes,
        agent_arch=arch.planner.label,
        agent_fingerprint=planner_executer_fingerprint(arch.planner, functions),
        arch=arch,
        tools=list(functions),
        # The engine has no `report_step` to cite knowledge in. Orchestration confirms uncited
        # usage rows only for runs that could cite (`citation_reporting = 'true'`).
        citation_reporting=False,
        compaction_model=None,
        compaction_prompt_version=None,
        # executer 와 macro 의 `ask`·`decide` 가 묻는 판단 model. tool loop 와 같은 값을 남긴다.
        decision_model=resolve_decision_model(),
        git_sha=settings.git_sha,
        image_tag=settings.image_tag,
    )


def step_module_functions() -> tuple[str, ...]:
    """planner 의 macro 가 보낼 수 있는 tool (`macro.<name>`)."""
    return tuple(f"macro.{name}" for name in TOOL_NAMES)
