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
