"""The application's own log calls have to actually reach the console.

They did not. Nothing configured the root logger, so every `logger.info` under
`app.` was discarded — including the QA run's console trace, which was never
printed once. These pin the wiring so that cannot regress silently.
"""

import logging

from app.logging_config import configure_logging


def _reset_root() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "name", None) == "artel-console":
            root.removeHandler(handler)


def test_an_app_logger_emits_info(caplog) -> None:
    _reset_root()
    configure_logging("INFO")

    with caplog.at_level(logging.INFO, logger="app.agents.qa.runner"):
        logging.getLogger("app.agents.qa.runner").info("[QA] run starting")

    assert "[QA] run starting" in caplog.text


def test_the_root_logger_gets_a_handler() -> None:
    """The gap was structural: no handler anywhere, so nothing to emit through."""
    _reset_root()
    configure_logging("INFO")

    root = logging.getLogger()
    named = [h for h in root.handlers if getattr(h, "name", None) == "artel-console"]
    assert len(named) == 1
    assert root.level == logging.INFO


def test_calling_it_twice_does_not_double_the_handler() -> None:
    """`--reload` re-imports the app in the same process."""
    _reset_root()
    configure_logging("INFO")
    configure_logging("INFO")

    root = logging.getLogger()
    named = [h for h in root.handlers if getattr(h, "name", None) == "artel-console"]
    assert len(named) == 1


def test_noisy_third_party_loggers_are_held_at_warning() -> None:
    """Turning the root logger on would otherwise bury the run trace in httpx chatter."""
    _reset_root()
    configure_logging("INFO")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("watchfiles").level == logging.WARNING
