# Project Context

Fill this document during project initialization. Agents must verify commands against repository configuration before running them.

## Overview

- Product: artel-agent-server
- Primary users: Backend services and operators that request AI agent workflows.
- Core domain: AI agent orchestration server for scenario generation, QA execution, and bug report workflows.
- Runtime environment: Python 3.11+ FastAPI application.

## Architecture

- Entry points: `app.main:create_app`, `app.main:app`
- Main modules: `app/api`, `app/llm`
- Dependency direction: API routes stay thin; LLM provider clients depend on shared LLM schemas and configuration.
- External systems: GitHub repository `project-artel/artel-agent-server`; Jira project `ARTEL` via the `mcp-atlassian` MCP server; Insomnia collection repository `project-artel/insomnia-api`
- Persistent data: None yet.

## Commands

| Purpose | Command |
|---|---|
| Install dependencies | `python -m pip install -e ".[dev]"` |
| Run locally | `python -m uvicorn app.main:app --reload` |
| Format | TODO |
| Lint | TODO |
| Type-check | TODO |
| Unit tests | `python -m pytest` |
| Integration tests | TODO |
| Build | TODO |
| Set up Jira credentials | `cp .jira.env.example .jira.env` |

Jira access goes through the `mcp-atlassian` MCP server, declared in `.mcp.json`
at the repository root. Claude Code starts it on demand and asks for approval
the first time it connects.

Credentials live in `.jira.env`, which the server reads through `--env-file`.
Copy `.jira.env.example` and fill in `JIRA_URL`, `JIRA_USERNAME`, and
`JIRA_API_TOKEN`, issuing the token at
`https://id.atlassian.com/manage-profile/security/api-tokens`. `.gitignore`
excludes `.jira.env`; never commit it.

The server reads that file itself, so the setup does not depend on how Claude
Code was launched or on which shell exports the variables. Do not register a
`jira` server in user scope as well, or two copies start.

### Insomnia collections

API collections live in `project-artel/insomnia-api`, one YAML file per
repository (`agent-server.yaml` for this one), and reach people through
Insomnia's git sync. Publish changes with the `insomnia-sync` skill: it derives
the API surface from the running contract, writes the collection file, and
opens a PR. Do not push collections into a local Insomnia app over the
`insomnia` MCP server — that changes one machine and leaves no reviewable diff.
The MCP server's read tools remain useful for inspecting local state.

Environment variables are committed alongside the requests, so every consumer
gets working URLs on pull. Secrets are excluded: keep them in an Insomnia
private environment. The collection repository is currently public.

## Constraints

- Supported platforms: Windows development environment; Linux deployment target not defined yet.
- Compatibility requirements: Python 3.11 or newer.
- Performance constraints: TODO
- Security or privacy requirements: External LLM credentials must be provided through environment/configuration, not committed.

## Ownership

- Maintainers:
- Sensitive modules:
- Changes requiring explicit review:
