# API Documentation

`artel-agent-server` exposes its API contract as OpenAPI 3 through FastAPI.

## Local access

Start the server:

```bash
python -m uvicorn app.main:app --reload
```

Then open one of the following paths:

| Path | Purpose |
|---|---|
| `/docs` | Interactive Swagger UI for exploring and testing endpoints |
| `/redoc` | Read-only ReDoc API reference |
| `/openapi.json` | Machine-readable OpenAPI contract for CI, SDKs, and clients |

## Authoring rule

Every externally consumed endpoint must declare:

- a clear router path and HTTP method
- a domain tag
- Pydantic request and response models
- a `summary` and, when behavior is non-obvious, a `description`
- documented error responses before the endpoint is consumed by another service

The OpenAPI JSON generated from the running application is the current API contract. README examples, frontend clients, SDK clients, and test cases must stay aligned with it.

## Verification

```bash
python -m pytest tests/test_api.py
```

The API contract test verifies that OpenAPI metadata and the health endpoint are published at `/openapi.json`.
