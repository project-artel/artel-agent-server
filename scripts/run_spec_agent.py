"""Run the spec agent over a WordVenture fixture and write the sheet.

Not product code and not committed — a local runner, kept here rather than in a
session scratchpad because the scratchpad is wiped between sessions and this got
rewritten from scratch more than once.

    PYTHONPATH=. uv run python scripts/run_spec_agent.py wv-editor ALL AB
    PYTHONPATH=. uv run python scripts/run_spec_agent.py wv-devbuild TitleScene A

    <build>   wv-editor | wv-devbuild
    <scenes>  ALL, or a comma-separated list of scene ids (TitleScene,Map_scene)
    <grades>  letters to include, default AB

The sheet lands in `output/<build>-<scenes>-<grades>-latest.json`, overwritten
every run. That file is the point: reopening the last result must not cost
another agent run — and unlike the CSV it used to write, it carries which spec
and which steps each row came from.

To read it as a spreadsheet:

    python -m app.affordance.csv output/wv-editor-ALL-AB-latest.json

`SPEC_MODEL` overrides the model by `LLMModel` member name.
`LANGSMITH_TRACING=false` silences the tracing client when its quota is spent.
"""

import asyncio
import json
import os
import pathlib
import sys

from app.affordance.document import documents_from
from app.agents.base import AgentContext
from app.agents.spec import SpecAgent
from app.agents.spec.agent import POLISH_BY_DEFAULT
from app.agents.spec.prompt import POLISH_AGENT, PROMPT_AGENT
from app.agents.spec.schemas import SPEC_MODEL
from app.prompts import resolve_version
from app.agents.spec.validation import validate_document
from app.llm.models import LLMModel
from tests.affordance_fixtures import preprocessed, report

OUTPUT = pathlib.Path("output")

# Long enough for a whole report. The endpoint's own 300s deadline is about one
# HTTP request; this is a person watching a terminal.
DEADLINE_SECONDS = 3600


def _report_wrong_screens(rows: list, specs: list, vocabulary: list) -> None:
    """Which rows the screen check counts, quoted. The count alone says nothing
    about whether a row is a false spec or only a second wording for one name."""
    from app.agents.spec.errors import SpecGenerationError
    from app.agents.spec.validation import _check_scene_targets, scene_wordings

    by_id = {spec.id: spec for spec in specs}
    wordings = scene_wordings(specs, vocabulary)
    for row in rows:
        spec = by_id.get(row.spec_id)
        if spec is None:
            continue
        try:
            _check_scene_targets(row, spec, wordings)
        except SpecGenerationError as error:
            print(f"  ! {row.spec_id} {row.used_step_indexes} — {error}")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2

    name = argv[1]
    wanted = set(argv[2].split(",")) if argv[2] != "ALL" else None
    grades = set(argv[3]) if len(argv) > 3 else {"A", "B"}
    model = LLMModel[os.environ["SPEC_MODEL"]] if os.environ.get("SPEC_MODEL") else SPEC_MODEL

    parsed = report(name)
    specs = [
        spec
        for spec in preprocessed(name).specs
        if (wanted is None or spec.trigger.scene in wanted) and spec.grade in grades
    ]
    print(f"{name} / {argv[2]} / 등급 {sorted(grades)} / 모델 {model.name} → 명세 {len(specs)}건")
    if (provenance := preprocessed(name).provenance) and provenance.build:
        print(
            f"  출처: {provenance.capture} · {provenance.build.platform} · "
            f"Unity {provenance.build.unity} · SDK {provenance.build.sdk} · "
            f"evidence {provenance.build.evidence}"
        )
    if not specs:
        return 1

    agent = SpecAgent(deadline_seconds=DEADLINE_SECONDS)
    rows = asyncio.run(
        agent.run(
            specs,
            set(parsed.scenes),
            AgentContext(session_id="local-run", metadata={}),
            model=model,
            locale="ko",
        )
    )
    print(f"행 {len(rows)}개 · agent reasons {dict(agent.reasons)}")
    print("문서 지표:", validate_document(rows, specs, agent.vocabulary))
    _report_wrong_screens(rows, specs, agent.vocabulary)

    vocabulary = {(entry.scene, entry.source): entry.wording for entry in agent.vocabulary}
    documents = documents_from(
        preprocessed(name),
        rows,
        specs,
        vocabulary,
        # Names the passes that actually ran. Stamping "+polish.v3" on a sheet
        # the polish pass never touched is the one lie provenance cannot afford.
        prompt_version=(
            f"spec.{resolve_version(PROMPT_AGENT)}"
            + (f"+polish.{resolve_version(POLISH_AGENT)}" if POLISH_BY_DEFAULT else "")
        ),
        llm_model=model.value,
    )

    OUTPUT.mkdir(exist_ok=True)
    scope = f"{argv[2].replace(',', '_')}-{''.join(sorted(grades))}"
    path = OUTPUT / f"{name}-{scope}-latest.json"
    path.write_text(json.dumps(documents, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
