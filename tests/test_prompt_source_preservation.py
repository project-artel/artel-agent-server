"""과거 prompt 판본과 충돌 갈래의 source bytes가 통합 중 바뀌지 않는 계약."""

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "docs/pr-consolidation-prompt-sources.json").read_text())


def test_runtime_versions_keep_the_pinned_source_bytes_and_exact_file_sets():
    for snapshot in MANIFEST["runtime"]:
        prefix = ROOT / "app/prompts/qa_run" / snapshot["version"]
        names = {str(path.relative_to(ROOT)) for path in prefix.rglob("*") if path.is_file()}
        assert names == set(snapshot["files"]), snapshot["version"]
        assert len(snapshot["sourceSha"]) == 40
        for name, expected in snapshot["files"].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name


def test_conflicting_unmerged_versions_keep_both_original_sources():
    assert {(row["sourcePr"], row["version"]) for row in MANIFEST["archives"]} == {
        (266, "v25"), (266, "v26"), (275, "v25"), (275, "v26")}
    for snapshot in MANIFEST["archives"]:
        assert len(snapshot["sourceSha"]) == 40
        assert snapshot["files"]
        for name, expected in snapshot["files"].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
