"""Every prompt this repository ships, held to the hash that was committed.

`app/prompts/lock.py` carries the why. What this file decides is the shape of
the failure: the three ways the tree and the lock can disagree are three
different mistakes with three different fixes, so they get three tests. Folded
into one assertion they would all report as "the lock is wrong" and leave the
reader to work out which of "make a new version", "regenerate", and "restore the
file" they were being told to do.

The messages matter more than usual here. Whoever sees one of these is mid-merge
and did not set out to touch a released prompt.
"""

import json

import pytest

from app.prompts import loader
from app.prompts.lock import LOCK_NAME, REGENERATE, compute_lock, read_lock
from app.prompts.loader import PromptError


def test_a_released_prompt_body_never_changes() -> None:
    """The one git cannot catch.

    A branch that adds `v9` and a branch that appends a paragraph to `v7` touch
    different files and merge clean. After that, runs recorded before and after
    the merge both say `prompt_version=v7` while having read different text, and
    any comparison across them silently averages the two.
    """
    locked = read_lock()["prompts"]
    current = compute_lock()["prompts"]

    changed = sorted(key for key in locked.keys() & current.keys() if locked[key] != current[key])

    assert not changed, (
        f"Prompt bodies changed under a version that already shipped: {changed}. "
        f"A version names one prompt and past runs are filed under that name, so "
        f"add a new version directory instead of editing this one. Only when the "
        f"version has never shipped is rewriting it right — then {REGENERATE}"
    )


def test_every_prompt_on_disk_is_in_the_lock() -> None:
    """An unlocked prompt is one nobody would notice the next edit to.

    This fires for a new version directory, and also for a new role file inside
    an existing one — `roles_in` treats any `.md` as a role, so `v8/notes.md`
    would be loaded as a prompt and has to be pinned like the rest.
    """
    locked = read_lock()["prompts"]
    current = compute_lock()["prompts"]

    unlocked = sorted(current.keys() - locked.keys())

    assert not unlocked, (
        f"Prompts on disk that the lock does not cover: {unlocked}. "
        f"Adding a version is deliberate work, and recording it is part of that "
        f"work — until it is in the lock, nothing guards it. {REGENERATE}"
    )


def test_the_lock_never_loses_a_prompt() -> None:
    """Deleting a released version is the same damage as editing one.

    The runs are still out there filed under it. Removing the file leaves a
    `prompt_version` nobody can resolve back to text, which is worse than a
    version that merely went stale.
    """
    locked = read_lock()["prompts"]
    current = compute_lock()["prompts"]

    missing = sorted(locked.keys() - current.keys())

    assert not missing, (
        f"Prompts in the lock with no file on disk: {missing}. Past runs are "
        f"filed under these versions; restore the files. Dropping one is only "
        f"safe once nothing refers to it, and then the lock entry goes with it."
    )


# --- reading a lock that is not one --------------------------------------------


@pytest.fixture
def lock_root(tmp_path, monkeypatch):
    """Point the loader — and with it the lock — at a throwaway directory."""
    monkeypatch.setattr(loader, "PROMPTS_ROOT", tmp_path)
    return tmp_path


def test_a_lock_left_with_conflict_markers_says_how_to_fix_it(lock_root) -> None:
    """The way this file actually breaks, so the message cannot be a stack trace.

    Two branches each adding a version is when the lock conflicts, and it is the
    same moment someone is tempted to resolve it by hand. Landing there with a
    JSONDecodeError and a column number does not tell anyone that the answer is
    to regenerate.
    """
    (lock_root / LOCK_NAME).write_text('{\n<<<<<<< HEAD\n  "version": 1\n}\n')

    with pytest.raises(PromptError, match="not valid JSON"):
        read_lock()


def test_a_lock_written_by_another_format_version_is_refused(lock_root) -> None:
    """Comparing entries written under a different shape is worse than not comparing."""
    (lock_root / LOCK_NAME).write_text(json.dumps({"version": 99, "prompts": {}}))

    with pytest.raises(PromptError, match="lock format version"):
        read_lock()


def test_a_missing_lock_is_an_error_rather_than_an_empty_one(lock_root) -> None:
    """An absent lock read as `{}` would let every check pass by locking nothing."""
    with pytest.raises(PromptError, match=REGENERATE):
        read_lock()
