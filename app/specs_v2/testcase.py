"""Projected specs, reshaped as `test-case.v1` records.

One record per spec, self-describing: `schema_version` rides on the record rather
than only on the envelope, so a row stays readable after it is stored on its own.

Nothing is decided here. Every field is copied from a projection that already
happened, or from run configuration. The transform adds exactly one derived
value — `used_step_indexes` — and it is derived from the scenario's own contract
list, not from anything about the sentence.
"""

from __future__ import annotations

from typing import Any

from .model import DiscoveryResult
from .render import artifact_label

SCHEMA_VERSION = "test-case.v1"

# `render` joins list-valued columns with this before they reach a CSV cell. The
# record form takes them apart again: a consumer that stores these should get a
# list, not a string it has to split on a separator it had to learn about.
JOIN = " / "


def _split(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(JOIN) if part.strip()]


def _used_step_indexes(row: dict[str, str], contracts_of: dict[str, list[str]]) -> list[int]:
    """Which of the scenario's contracts this row spoke for, by position.

    A projected row can carry contracts from scenarios it absorbed as well as its
    own (`covered_spec_ids` names those scenarios). Only the scenario's own list
    is indexable, so a borrowed contract contributes no index — it is already
    accounted for by `covered_spec_ids`, and inventing a position for it would
    make the index mean two different things.
    """
    own = contracts_of.get(_scenario_of(row["spec_id"]), [])
    position = {contract_id: index for index, contract_id in enumerate(own)}
    used = [position[contract_id] for contract_id in _split(row.get("contract_ids")) if contract_id in position]
    return sorted(set(used))


def _scenario_of(spec_id: str) -> str:
    """`scenario:52b501134eca:1` → `scenario:52b501134eca`.

    A scenario that projects into several rows suffixes each one, and the suffix
    is not part of the scenario's identity.
    """
    parts = spec_id.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else spec_id


def test_case(
    row: dict[str, str],
    *,
    contracts_of: dict[str, list[str]],
    scene_keys: dict[str, str | None],
    artifact: str,
    prompt_version: str | None,
    llm_model: str | None,
) -> dict[str, Any]:
    scenario_id = _scenario_of(row["spec_id"])
    return {
        "schema_version": SCHEMA_VERSION,
        "spec": {
            "scene": row["scene"],
            "precondition": row["precondition"],
            "step": row["test_step"],
            "expected_value": row["expected_result"],
            "status": row["status"],
        },
        "metadata": {
            "source": {
                "spec_id": row["spec_id"],
                # The SDK's own scene name, or `null` when the scan could not
                # place the evidence. `spec.scene` says `미확정` there because a
                # sentence needs a word; a key must not invent one.
                "scene_key": scene_keys.get(scenario_id),
                "used_step_indexes": _used_step_indexes(row, contracts_of),
                "evidence_gaps": _split(row.get("review_reason")),
            },
            "generation": {
                "build_evidence": row["build_evidence"] or None,
                # `editor` or `devbuild` — what a person building the game would
                # call the artifact, not the SDK's `capture` provenance, which
                # says `player` for both a development and a shipping build.
                "capture": artifact,
                "prompt_version": prompt_version,
                "llm_model": llm_model,
            },
        },
    }


def test_cases(
    result: DiscoveryResult,
    rows: list[dict[str, str]],
    *,
    prompt_version: str | None = None,
    llm_model: str | None = None,
) -> list[dict[str, Any]]:
    contracts_of = {scenario.id: list(scenario.contracts) for scenario in result.scenarios}
    scene_keys = {scenario.id: scenario.scene for scenario in result.scenarios}
    artifact = artifact_label(result)
    return [
        test_case(
            row,
            contracts_of=contracts_of,
            scene_keys=scene_keys,
            artifact=artifact,
            prompt_version=prompt_version,
            llm_model=llm_model,
        )
        for row in rows
    ]
