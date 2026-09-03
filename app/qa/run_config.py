"""What a QA run was actually executed with, settled before the run starts.

A run is comparable to another run only if what each was executed with is
recorded. The request is not that record: several of its fields are wishes
rather than facts.

* ``prompt_version: null`` means "the newest version", which is an alias whose
  meaning changes the day a ``v4`` directory is added. Every run recorded under
  the alias becomes unattributable at that moment, including the ones already
  finished.
* ``reasoning: null`` means either "not asked for" or "this model has none", and
  a comparison that cannot tell those apart will read a model's missing
  capability as a choice nobody made.
* ``arch.vision: "auto"`` is a question about the model, not an answer.

So everything is resolved once, here, at session open — before a channel exists
and before the run begins — and the resolved form is what is stored, logged,
returned to Orchestration, and eventually grouped by.
"""

from pydantic import BaseModel, ConfigDict

from app.agents.qa.arch import (
    DEFAULT_ARCH,
    QaArchSpec,
    ResolvedArch,
    resolve_arch,
    structure_of,
)
from app.agents.scenario import DEFAULT_LANGUAGE, OutputLanguage
from app.config import get_settings
from app.llm.chat_model import TEMPERATURE
from app.llm.models import (
    DEFAULT_MODEL,
    LLMModel,
    ReasoningConfig,
    get_model_spec,
    validate_reasoning,
)
from app.prompts import load_prompt

# Directories under app/prompts/ holding these agents' prompt versions. The
# summarizing prompt is versioned apart from the run's own: it is a different call
# to a different model, so pinning one back must not pin the other.
PROMPT_AGENT = "qa_run"
COMPACTION_PROMPT_AGENT = "qa_compaction"

# The roles that make up the run's system prompt. `vision_directive` only reaches
# a run that can see — telling a text-only model about a tool it does not have
# sends it looking for one — so it is hashed only when it is used.
SYSTEM_ROLE = "system"
VISION_ROLE = "vision_directive"
COMPACTION_ROLE = "summary"

# This build reports citations. Declared as a constant rather than written inline
# so that a build which one day cannot — a stripped tool set, a structure without
# `report_step` — has one place to say so instead of a literal to hunt for.
CITATION_REPORTING = True


class RunConfig(BaseModel):
    """One run's execution settings, with nothing left to interpret."""

    model_config = ConfigDict(frozen=True)

    model: LLMModel
    provider: str
    temperature: float
    reasoning: ReasoningConfig | None
    # Distinguishes "no reasoning was asked for" from "this model has none".
    # Without it a null above has two meanings and the comparison silently
    # merges them.
    reasoning_supported: bool
    language: OutputLanguage
    # Resolved: the version that was actually loaded, never the alias.
    prompt_version: str
    # role -> sha256 of the body, so an edit to a version in place is visible as
    # a different bucket instead of quietly averaging two prompts under one name.
    prompt_hashes: dict[str, str]
    agent_arch: str
    agent_fingerprint: str
    arch: ResolvedArch
    tools: list[str]
    # Whether this build's `report_step` can report which knowledge a verdict
    # rested on (ARTEL-293/294).
    #
    # Constant `True` here, and that is the point: Orchestration confirms every
    # run's uncited `knowledge_usage` rows as `cited=false` when the try ends, and
    # it must not do that to a run that had no way to report a citation. Those two
    # cases have to stay apart — `null` is "could not report", `false` is "could
    # and did not" — and the only honest way to tell them apart later is a mark
    # left on the row at the time. A run from before this field simply has no key
    # in `run_config`, so it stays null forever, which is correct.
    #
    # Not derived from `prompt_version` on the far side: that would tie a data
    # question to a version-numbering scheme, and it cannot answer at all for a
    # run whose Agent never reported its settings.
    citation_reporting: bool
    # Compaction summarizes with its own model and its own prompt version, so a
    # run's context can be rewritten by something the run's own model and prompt
    # axes do not describe. Null when the structure has compaction off.
    compaction_model: str | None
    compaction_prompt_version: str | None
    # Null outside a built image. Reported as null rather than guessed.
    git_sha: str | None
    image_tag: str | None


def resolve_run_config(
    model: LLMModel = DEFAULT_MODEL,
    language: OutputLanguage = DEFAULT_LANGUAGE,
    prompt_version: str | None = None,
    reasoning: ReasoningConfig | None = None,
    arch: QaArchSpec = DEFAULT_ARCH,
) -> RunConfig:
    """Settle every axis of one run, or refuse the request.

    Raises ``ValueError`` (``QaArchError`` is one) when the request cannot be
    honoured as asked — a reasoning budget the model has no setting for, a
    ``vision="on"`` it cannot satisfy. Refusing beats quietly downgrading:
    a run filed under a structure it did not have is a wrong data point, and
    wrong is worse than absent in the one direction nobody re-checks.

    Prompts are loaded here rather than at run start, so the resolved version is
    known while the session is still being opened and a broken prompt file fails
    the open instead of the run.
    """
    reasoning = validate_reasoning(model, reasoning)
    spec = get_model_spec(model)
    resolved_arch = resolve_arch(arch, model)
    tools, _middleware, fingerprint = structure_of(resolved_arch)

    prompt = load_prompt(PROMPT_AGENT, SYSTEM_ROLE, prompt_version)
    hashes = {SYSTEM_ROLE: prompt.body_sha256}
    if resolved_arch.vision:
        # Pinned to the version just resolved, not to the caller's alias: the two
        # halves of one prompt have to come from the same version.
        hashes[VISION_ROLE] = load_prompt(
            PROMPT_AGENT, VISION_ROLE, prompt.version
        ).body_sha256

    settings = get_settings()
    # 설정이 비어 있으면 런의 모델로 압축한다. 그래야 압축이 런과 같은 provider 를
    # 쓰고, 한쪽 credit 이 없을 때 압축만 조용히 실패하는 일이 없다.
    compaction_model = (
        (settings.qa_compaction_model or model.value)
        if resolved_arch.compaction
        else None
    )
    compaction_prompt = (
        load_prompt(
            COMPACTION_PROMPT_AGENT, COMPACTION_ROLE, settings.qa_compaction_prompt_version
        )
        if resolved_arch.compaction
        else None
    )
    if compaction_prompt is not None:
        hashes[COMPACTION_ROLE] = compaction_prompt.body_sha256

    return RunConfig(
        model=model,
        provider=spec.provider.value,
        temperature=TEMPERATURE,
        reasoning=reasoning,
        reasoning_supported=spec.reasoning is not None,
        language=language,
        prompt_version=prompt.version,
        prompt_hashes=hashes,
        agent_arch=resolved_arch.label,
        agent_fingerprint=fingerprint,
        arch=resolved_arch,
        tools=list(tools),
        citation_reporting=CITATION_REPORTING,
        compaction_model=compaction_model,
        compaction_prompt_version=compaction_prompt.version if compaction_prompt else None,
        git_sha=settings.git_sha,
        image_tag=settings.image_tag,
    )
