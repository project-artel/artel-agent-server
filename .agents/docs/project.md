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
- External systems: GitHub repository `project-artel/artel-agent-server`; Jira project `ARTEL` via the `mcp-atlassian` MCP server; Insomnia collection repository `project-artel/insomnia-api`; LangSmith tracing (opt-in, see README)
- Persistent data: None yet.

## API 표면과 신뢰 경계

이 서버에는 공개 표면이 없다. 업무 라우트는 전부 orchestration만 부르는
서버-투-서버 경로이며 `/internal` 아래 산다. 인증은 없고, 노출을 막는 것은
경로가 아니라 배포 구성이다 — 컨테이너는 `app-net`에만 붙고 `docker run`에
`-p`가 없으며 리버스 프록시가 이 서비스를 가리키는 공개 호스트를 갖지 않는다.

- `/internal/**` — 업무 라우트. orchestration 전용, 무인증
- `/health`, `/docs`, `/redoc`, `/openapi.json` — 접두사 없이 둔다.
  컨테이너 헬스체크·문서 진입점이며 신뢰 경계와 무관하다

규칙 셋:

1. 새 서버-투-서버 라우트는 `/internal` 아래에 붙인다.
2. **공개 라우트를 처음 추가하는 사람이 인증 경계를 세운다.** 접두사 밖에
   붙이고, 그 시점에 엔드유저 인증을 설계한다. 무인증 라우트를 `/internal`
   밖에 두지 않는다.
3. 배포에 `-p`를 추가하거나 이 서비스를 프록시하는 공개 호스트를 만들지
   않는다. 그 순간 `/internal` 전체가 인터넷에서 무인증으로 열린다.

orchestration은 같은 규칙으로 가는 중이다(ARTEL-265). 이 문서를 쓰는 시점의
`develop`에는 `/internal` 라우트가 없고 전 컨트롤러가 `/api/**`에 있다 — 이
서버가 사용량을 보내는 곳도 아직 `/api/orchestration/llm-usage`다
(`app/llm/usage.py`). 그쪽은 공개 API가 같은 앱에 있어 접두사가 실제로 둘을
가르게 되지만, 별도 포트로 분리하는 방향(ARTEL-266)도 진행 중이라 최종 형태는
아직 확정이 아니다. 이 서버의 규칙은 그 결과와 무관하게 위와 같다.

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
opens a PR.

Do not publish by writing into a local Insomnia app — neither through the
`insomnia` MCP server's write tools nor by editing the `insomnia.*.db` NeDB
store. Either way only one machine changes and no reviewable diff exists.
Reading local state is fine.

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
