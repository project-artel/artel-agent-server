"""Where the trust boundary sits, asserted as a surface rather than per-route.

Every business route is server-to-server traffic from orchestration and lives
under `/internal`. `/health` and the docs entry points do not, because they are
container and tooling surface. The cutover is hard, so the old unprefixed paths
must be gone rather than merely undocumented.

`test_health_is_the_only_unprefixed_path` is the drift guard for routes added
later — but only for HTTP ones. It reads the OpenAPI schema, and WebSocket
routes never appear there, so a WebSocket mounted outside the prefix would pass
this file unnoticed. FastAPI does not expose the effective path of a WebSocket
route once a prefix has been applied (it reads back empty), so closing that hole
would mean reaching into router internals and re-breaking on the next upgrade.
The gap is left open and named here instead: when adding a WebSocket route,
`.agents/docs/project.md`'s prefix rule is the thing enforcing it, not a test.

The old paths are checked at runtime, not just against the schema — absent from
the contract and actually returning 404 are different claims, and only the
second one is what orchestration will hit.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.qa.service import QaExecutionService
from app.qa.store import InMemoryQaSessionStore
from app.sessions.service import SessionService
from app.sessions.store import InMemorySessionStore

# The literal, not `app.main.INTERNAL_PREFIX`. Importing the constant would make
# this file move with it and assert nothing.
PREFIX = "/internal"

# The whole HTTP surface that moved, as (method, path-without-prefix). The
# sub-paths are listed individually: a router mount is unlikely to drop one, but
# "unlikely to break" and "checked" are not the same claim.
MOVED_HTTP_ROUTES = [
    ("post", "/sessions"),
    ("post", "/sessions/{session_id}/approve"),
    ("post", "/sessions/{session_id}/decline"),
    ("post", "/qa-sessions"),
    ("post", "/extract"),
    ("post", "/embed"),
    ("post", "/knowledge-queries"),
    ("get", "/models"),
    ("post", "/specs/v2/generate"),
]

# WebSocket routes never appear in the OpenAPI schema, so they are covered by
# connecting instead.
MOVED_WS_ROUTES = [
    "/sessions/{session_id}",
    "/qa-sessions/{session_id}",
]

client = TestClient(app)


@pytest.fixture
def wired_services():
    """Install the services the WebSocket handlers read, minus Redis.

    Both handlers resolve their service off `app.state` *before* accepting the
    socket, so an unwired app fails the connection with `AttributeError` and the
    test would prove nothing about routing. The app's lifespan wires them
    against a live Redis, so it is bypassed and the in-memory stores stand in —
    the same trade `tests/test_qa_run_config_contract.py` makes.
    """
    app.state.session_service = SessionService(store=InMemorySessionStore())
    app.state.qa_session_service = QaExecutionService(store=InMemoryQaSessionStore())
    yield
    del app.state.session_service
    del app.state.qa_session_service


def _schema() -> dict:
    return client.get("/openapi.json").json()


def test_every_business_route_is_published_under_the_prefix() -> None:
    paths = _schema()["paths"]

    for method, path in MOVED_HTTP_ROUTES:
        prefixed = f"{PREFIX}{path}"
        assert prefixed in paths, f"{prefixed} is not mounted"
        assert method in paths[prefixed], f"{prefixed} lost its {method.upper()}"


def test_health_is_the_only_unprefixed_path() -> None:
    """The guard for HTTP routes added later.

    `/docs`, `/redoc`, and `/openapi.json` are served outside the schema and
    never appear in `paths`, so the set really is this small. Comparing the
    whole set rather than checking `/health` alone is what makes a future HTTP
    route mounted without the prefix fail here instead of shipping. WebSocket
    routes are not in the schema and so are not covered — see the module
    docstring.
    """
    unprefixed = {
        path for path in _schema()["paths"] if not path.startswith(f"{PREFIX}/")
    }

    assert unprefixed == {"/health"}


def test_health_still_answers_without_the_prefix() -> None:
    """The container health check depends on this exact path."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(("method", "path"), MOVED_HTTP_ROUTES)
def test_the_old_http_path_is_gone(method: str, path: str) -> None:
    """Hard cutover: no alias, no dual mount, no transition window."""
    concrete = path.replace("{session_id}", "any-session")

    response = getattr(client, method)(concrete)

    assert response.status_code == 404


@pytest.mark.parametrize("path", MOVED_WS_ROUTES)
def test_the_old_websocket_path_is_gone(path: str) -> None:
    """Starlette closes an unrouted socket rather than answering 404.

    `routing.py` sends `WebSocketClose()` for a scope that matches nothing, and
    the test client surfaces that as `WebSocketDisconnect` while entering the
    context — so this is neither an HTTP status nor a denial response.
    """
    concrete = path.replace("{session_id}", "any-session")

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(concrete):
            pass


def test_the_scenario_socket_accepts_under_the_prefix(wired_services) -> None:
    """An unknown id is enough: the error frame proves the socket was accepted.

    What the handler does with the session belongs to `tests/test_sessions.py`.
    All this needs to show is that the route resolves at the new path.
    """
    with client.websocket_connect(f"{PREFIX}/sessions/unknown-session") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "error"
    assert frame["code"] == "session_expired"


def test_the_qa_socket_accepts_under_the_prefix(wired_services) -> None:
    with client.websocket_connect(f"{PREFIX}/qa-sessions/unknown-session") as ws:
        frame = ws.receive_json()

    assert frame["type"] == "ERROR"
    assert frame["payload"]["code"] == "session_expired"
