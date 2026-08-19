"""Generate a static OpenAPI snapshot for the agent-server API.

Runs inside CI after `pip install -e .`. Imports the FastAPI app and dumps
`/openapi.json` to `docs/api/openapi.json`, then CI diffs that file.

The lifespan (Redis, clients) is only run when the ASGI server starts; we only
build the schema, so no external connection is made here.
"""

import json
from pathlib import Path

from app.main import app


def main() -> None:
    target = Path("docs/api/openapi.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    spec = app.openapi()
    target.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
    print(f"wrote {target} ({sum(len(json.dumps(p)) for p in spec.get('paths', {}))} paths)")


if __name__ == "__main__":
    main()