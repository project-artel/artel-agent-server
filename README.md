# artel-agent-server

Python FastAPI backend skeleton for operating AI agents.

The current skeleton intentionally keeps API surface small. It includes a
health endpoint and the first LLM client abstraction for OpenRouter-backed
chat completions.

## Local Development

Create a local environment file from the example:

```powershell
Copy-Item .env.example .env
```

Then set `OPENROUTER_API_KEY` in `.env`.
The application loads `.env` automatically through `app.config.Settings`.

Install dependencies:

```powershell
python -m pip install -e ".[dev]"
```

Run the API:

```powershell
python -m uvicorn app.main:app --reload
```

Run tests:

```powershell
python -m pytest
```

## Container

Build the image:

```powershell
docker build --target runtime -t artel-agent-server:local .
```

Run tests through the Dockerfile:

```powershell
docker build --target test -t artel-agent-server:test .
```

Run the container:

```powershell
docker run --rm -p 8080:8080 -v ${PWD}/.env:/app/.env:ro artel-agent-server:local
```

## Jenkins

`Jenkinsfile` resolves deployment target from the branch name:

- `main`, `operation` -> `operation`
- `develop`, `stage` -> `stage`

Pull request builds are detected through Jenkins multibranch variables such as
`CHANGE_ID`. PR builds run the Dockerfile `test` target but do not build or
deploy the runtime container.

The Jenkins workspace should provide `.env.stage` and `.env.operation` files.
The pipeline mounts the selected file into the container as `/app/.env`.
The Jenkins host should provide Docker and the `app-net` Docker network.
