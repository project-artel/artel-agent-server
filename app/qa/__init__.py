"""QA execution session management (envelope, store, service, schemas).

Note: ``service``, ``schemas`` and ``run_config`` are intentionally not
re-exported here. Each depends on ``app.agents.qa``, which in turn imports
``app.qa.envelope`` — importing any of them at package-init time would form a
circular import. Import them directly::

    from app.qa.service import QaExecutionService
    from app.qa.schemas import QaSessionRecord
    from app.qa.run_config import RunConfig

``schemas`` joined that list when ``QaSessionRecord`` began carrying the run's
resolved ``RunConfig``, which is built from ``app.agents.qa.arch``. Everything
in the codebase already imports these by module path, so the rule costs nothing
but has to be stated: re-adding one here reopens the cycle.
"""

from app.qa.envelope import GameState, MessageType
from app.qa.store import (
    InMemoryQaSessionStore,
    QaSessionStore,
    RedisQaSessionStore,
)

__all__ = [
    "GameState",
    "InMemoryQaSessionStore",
    "MessageType",
    "QaSessionStore",
    "RedisQaSessionStore",
]
