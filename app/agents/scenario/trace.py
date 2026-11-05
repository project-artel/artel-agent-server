"""What only this side can see, written where the other side's record already is (ARTEL-650).

Orchestration keeps a per-run file of everything that crosses the boundary — every
question the turn asks, every answer it gets, every scenario it returns, and what
happens to that answer afterwards. Two things never cross that boundary:

  * the prompt the model actually received (orchestration sends the pieces, never
    the assembled text), and
  * the turn's verdict on its own ordering, which decides whether it asks again.

Both live here. So this writes into the *same* file, keyed by the same run — which is
why the directory is a shared setting rather than a log of our own. Reading one file
top to bottom is the whole point; a second file in a second place is a second thing to
correlate by hand, and that is the work this was meant to remove.

Off by default, and never raises. A record kept for hindsight must not be able to stop
the turn it is recording.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("ARTEL_SCENARIO_TRACE_ENABLED", "").lower() in ("1", "true", "yes")


def _dir() -> Path:
    return Path(os.getenv("ARTEL_SCENARIO_TRACE_DIR", ".trace"))


def record(run_id: int | None, event: str, detail: str | None = None) -> None:
    """One event, indented under its heading, appended to this run's file."""
    if run_id is None or not _enabled():
        return
    try:
        body = f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {event}"
        if detail and detail.strip():
            for line in detail.rstrip().splitlines():
                body += f"\n              {line}"
        directory = _dir()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"run-{run_id}.log").open("a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    except Exception as error:  # noqa: BLE001 — hindsight must not break the turn
        logger.debug("[scenario] trace failed: %s", error)


def blob(run_id: int | None, name: str, content: str) -> str:
    """Park something too long for the file body beside it; return the line to write."""
    if run_id is None or not _enabled():
        return ""
    try:
        directory = _dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"run-{run_id}-{name}"
        path.write_text(content, encoding="utf-8")
        return f"→ {path.name} ({len(content)}자)"
    except Exception as error:  # noqa: BLE001
        logger.debug("[scenario] trace blob failed: %s", error)
        return ""
