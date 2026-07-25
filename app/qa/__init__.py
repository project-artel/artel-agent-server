"""QA execution session management (envelope, store, service, schemas).

Note: ``service`` is intentionally not re-exported here. It depends on
``app.agents.qa``, which in turn imports ``app.qa.envelope`` — importing the
service at package-init time would form a circular import. Import it directly:
``from app.qa.service import QaExecutionService``.
"""

from app.qa.envelope import GameState, MessageType
from app.qa.schemas import QaSessionRecord, QaStepResult
from app.qa.store import (
    InMemoryQaSessionStore,
    QaSessionStore,
    RedisQaSessionStore,
)

__all__ = [
    "GameState",
    "InMemoryQaSessionStore",
    "MessageType",
    "QaSessionRecord",
    "QaSessionStore",
    "QaStepResult",
    "RedisQaSessionStore",
]
