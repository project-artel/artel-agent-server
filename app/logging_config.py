"""Console logging for the application's own loggers.

uvicorn configures only the `uvicorn.*` loggers. Nothing configures the root
logger, so every `logger.info` under `app.` had no handler to reach and Python's
last-resort handler only emits WARNING and above — the calls simply vanished.

That is not a cosmetic gap. The QA run's console trace (the prompt the model was
given, each model turn, each tool result) is the only place some of that
information exists at all, and none of it had ever been printed.
"""

import logging
import sys

_HANDLER_NAME = "artel-console"

# Third-party INFO chatter that would bury the application's own lines once the
# root logger starts emitting INFO. Raised to WARNING rather than silenced, so a
# real failure in any of them still surfaces.
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "openai",
    "urllib3",
    "asyncio",
    "watchfiles",
)


def configure_logging(level: str = "INFO") -> None:
    """Attach one console handler to the root logger.

    Idempotent: `--reload` re-imports the application in the same process, and a
    second handler would double every line.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for logger_name in _NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    for handler in root.handlers:
        if getattr(handler, "name", None) == _HANDLER_NAME:
            handler.setLevel(level)
            return

    handler = logging.StreamHandler(sys.stdout)
    handler.name = _HANDLER_NAME
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
