"""A committed hash of every prompt this repository ships.

A version directory is a promise: ``qa_run/v7`` names one prompt, and every run
filed under ``prompt_version=v7`` read that exact text. ``loader`` says why
editing a released version in place breaks the promise. This module is what
stops it from happening quietly.

Git does not stop it. One branch adding ``v9`` and another appending a paragraph
to ``v7`` touch different files, merge clean, and produce no conflict for anyone
to notice. Neither do the version-to-version tests in
``tests/test_qa_prompt_version.py``: they assert that a newer version still
contains every paragraph of the older one, which an *addition* satisfies.

So the check compares the tree against committed data rather than against
history. It also has to — CI runs pytest inside the image built by
``Dockerfile``'s ``test`` target, which copies ``app`` and ``tests`` and no git
directory, so there is nothing to diff against in there.

Regenerate after adding a version, and only then::

    python -m app.prompts.lock --write

Resolve a merge conflict in the lock file by re-running that, never by editing
the JSON. Two branches each adding a version is exactly when the conflict shows
up and exactly when hand-merging is tempting; a hash nobody computed locks
nothing, and it fails open.
"""

import json
import sys
from pathlib import Path

from app.prompts import loader
from app.prompts.loader import (
    PromptError,
    available_versions,
    known_agents,
    load_prompt,
    roles_in,
)

# Bumped only when the file's shape changes, which `read_lock` refuses to guess
# at: a lock it cannot parse the same way it was written is not a lock.
LOCK_VERSION = 1
LOCK_NAME = "prompts-lock.json"

REGENERATE = "Run `python -m app.prompts.lock --write` and commit the result."


def lock_path() -> Path:
    """Where the lock lives.

    Read from the module attribute at call time rather than bound at import,
    so a test that points the loader at a throwaway tree moves this too.

    It sits beside the prompts, and inside ``app/``, because that is what the
    test image copies. At the repository root — where ``skills-lock.json`` sits
    — it would simply be absent in CI, and the check would pass by not running.
    """
    return loader.PROMPTS_ROOT / LOCK_NAME


def compute_lock() -> dict:
    """What the lock should say about the prompts currently on disk.

    Keyed ``<agent>/<version>/<role>`` and carrying ``body_sha256``, which
    excludes the frontmatter: ``note`` is documentation, and rewording it does
    not change what the model read. ``v8`` shipping the same body as ``v7`` under
    a different note is a real case, so the hash has to be about the body alone.

    Every version is named explicitly, which keeps this off ``Settings`` —
    ``resolve_version`` only consults ``*_PROMPT_VERSION`` when no version is
    passed. A check that read the environment would pass or fail by deployment.
    """
    return {
        "version": LOCK_VERSION,
        "prompts": {
            f"{agent}/{version}/{role}": load_prompt(agent, role, version).body_sha256
            for agent in known_agents()
            for version in available_versions(agent)
            for role in roles_in(agent, version)
        },
    }


def read_lock() -> dict:
    """The committed lock, or an error naming the way out."""
    path = lock_path()
    if not path.is_file():
        raise PromptError(f"No prompt lock at {path}. {REGENERATE}")

    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        # The predicted way this file breaks. Two branches each add a version,
        # the lock conflicts, and someone leaves a marker in it — so the message
        # has to name the fix rather than surface a column number.
        raise PromptError(f"{path}: not valid JSON ({error}). {REGENERATE}") from error

    if lock.get("version") != LOCK_VERSION:
        raise PromptError(
            f"{path}: lock format version {lock.get('version')!r}, expected "
            f"{LOCK_VERSION}. This file was written by a different version of "
            f"{__name__} and its entries cannot be compared as they are."
        )
    return lock


def write_lock() -> Path:
    """Overwrite the lock with the current tree. Sorted, so diffs stay readable."""
    path = lock_path()
    path.write_text(
        json.dumps(compute_lock(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


if __name__ == "__main__":
    if "--write" not in sys.argv[1:]:
        raise SystemExit("usage: python -m app.prompts.lock --write")
    print(f"wrote {write_lock()}")
