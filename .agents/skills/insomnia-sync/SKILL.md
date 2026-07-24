---
name: insomnia-sync
description: >
  Analyzes the current repository's API surface and publishes it as an Insomnia
  collection to the shared `project-artel/insomnia-api` repository through a
  commit/push/PR — including environment variables. Replaces registering
  collections into a local Insomnia app, whether through MCP write tools or by
  editing the local NeDB store. Invoke when the user says "insomnia sync",
  "/insomnia-sync", "insomnia 반영", "API 인섬니아에 올려", "컬렉션 갱신", or
  "insomnia 컬렉션 PR".
---

Insomnia collections are source-controlled in `project-artel/insomnia-api`. Each
repository owns exactly one collection file, and every consumer receives it
through Insomnia's git sync. Changes reach people by merging a PR.

A local Insomnia app is never the publishing path. Writing into it — through the
MCP write tools or by appending to the `insomnia.*.db` NeDB store — changes one
machine, leaves no reviewable diff, and gives no way to tell when the collection
and the API drifted apart. Reading local state is fine and often useful.

## Steps

1. **Identify the collection file.** It is `<repo-slug>.yaml` at the root of
   `project-artel/insomnia-api`, where the slug drops a leading `artel-` from
   the repository name — `artel-agent-server` → `agent-server.yaml`,
   `artel-orchestration-server` → `orchestration-server.yaml`. If the file does
   not exist yet, this run creates it, starting from
   `assets/COLLECTION_TEMPLATE.yaml` and deleting the request shapes the service
   does not have.

2. **Derive the API surface from the repository, not from memory.** Prefer the
   generated contract over source reading:

   | Stack | Contract |
   |---|---|
   | FastAPI | `/openapi.json`, or import `app.main:create_app` and call `app.openapi()` |
   | Spring Boot + springdoc | `/v3/api-docs` while `./mvnw spring-boot:run` is up |

   When the app will not start, enumerate route declarations — `@router.<verb>`,
   `@GetMapping`/`@PostMapping`/`@RequestMapping`, `RouterFunction` — and their
   request/response models. Check whether the repository already keeps a written
   API summary (`docs/api-documentation.md`) and reconcile against it.

   For each endpoint capture: method, path, tag, summary, authentication, and a
   realistic example request body built from the actual schema — required
   fields, enum values spelled out, no invented properties.

3. **Clone the collection repository into the scratchpad** and branch from the
   default branch:

   ```bash
   git clone https://github.com/project-artel/insomnia-api.git
   cd insomnia-api && git checkout -b feat/<repo-slug>-<change>
   ```

   Never edit Insomnia's own clone under `<data-dir>/version-control/git/`,
   where `<data-dir>` is the app's per-platform storage:

   | Platform | Insomnia data directory |
   |---|---|
   | Windows | `%APPDATA%\Insomnia` |
   | macOS | `~/Library/Application Support/Insomnia` |
   | Linux | `~/.config/Insomnia` |

   That directory is the app's working copy; the app owns its state.

4. **Write the collection file.** What binds is Insomnia's own schema 5.1 — the
   top-level keys, the `wrk_`/`req_`/`ws-req_`/`env_` id prefixes,
   epoch-millisecond timestamps — because the app refuses to parse anything
   else. `assets/COLLECTION_TEMPLATE.yaml` is one filled-in example of that
   schema; consult it for the shape of a construct you are unsure how to
   express, not as a layout to reproduce. Its endpoints, `sortKey` values, and
   request mix are illustrative.

   Reconcile rather than regenerate: keep existing `meta.id` values for requests
   that still exist, since git sync matches rows by id and response-chaining
   references point at them. Add, update, and delete only what the API surface
   actually changed.

5. **Write each request's `description` as Markdown.** Insomnia renders it in
   the Docs panel, where a single newline collapses into the previous paragraph
   — separate blocks with a blank line, inside a `|-` block scalar. Answer who
   calls the endpoint and what the caller does with the result, not just its
   input and output:

   ```
   **호출 주체**: FE 대시보드(React) → Orchestration

   **동작**: 서버에서 무엇이 일어나는지 1–2문장.

   **Request**
   - `projectId` (number): 소속 프로젝트 id

   **Response**
   - `200`: `{ testScenarioId }`
   - `404`: 비참여자/미존재

   **활용**: 반환값을 이후 요청의 경로 키로 쓴다.
   ```

6. **Define environments in the same file.** Every URL must resolve without
   manual editing after a pull:
   - `environments.data` — shared defaults, pointing at staging.
   - `environments.subEnvironments` — one entry per deployment target
     (`local`, and `prod` where one exists), overriding the same variable names.
   - Variables are `stage_<service>_base_url` and `stage_<service>_ws_url`,
     following the `stage_agent_*` and `stage_orch_*` already in use. The
     `stage_` prefix names the Base Environment default, not the only target —
     a `local` sub-environment still overrides those same keys. That reads
     oddly; leave it until the team renames it across every collection at once.
   - Match the HTTP scheme to the WS scheme: `https` pairs with `wss`, `http`
     with `ws`.
   - Authenticated endpoints reference a token the collection does not define,
     e.g. a `Cookie: artel_access_token={{ _.access_token }}` header. Leaving
     `access_token` undefined is correct — see the rules below.

7. **Validate before committing.** Parse the file — a YAML error Insomnia would
   reject is the cheapest failure to catch here:

   ```bash
   python3 -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(len(d['collection']),'requests')" <file>.yaml
   ```

   Use `python` instead of `python3` on Windows, where the versioned name
   usually is not on `PATH`. PyYAML is not in the standard library and a
   JVM-only repository will not have it; install it into a throwaway
   environment rather than the system interpreter, or fall back to any YAML
   parser at hand — `ruby -ryaml -e 'YAML.load_file(ARGV[0])'` needs nothing
   extra on macOS.

   Then confirm every `{{ _.name }}` in the file resolves against a defined
   variable, secrets excepted:

   ```bash
   grep -oE '\{\{ *_\.[a-zA-Z0-9_]+ *\}\}' <file>.yaml | sort -u
   ```

   An unresolved variable renders as an empty string and the request silently
   fails — this is the failure mode the skill exists to prevent.

8. **Commit, push, and open a PR** against the default branch, following
   `.agents/docs/commit.md` and `.agents/docs/pull-request.md`. The PR body
   states which endpoints were added, changed, or removed, and which source
   commit of the origin repository the collection reflects.

9. **Report the PR URL.** Do not merge. After merge, consumers pull in Insomnia
   under Preferences → Git Sync; the collection appears in the git-linked
   project.

## Rules

- No secrets in the collection file. The repository is public. Tokens, API keys,
  passwords, and real user identifiers belong in an Insomnia private
  environment (`isPrivate: true`), which git sync excludes. Referencing
  `{{ _.access_token }}` without defining it is correct — each person fills it
  locally.
- Only non-sensitive infrastructure values get committed: hostnames, ports,
  placeholder ids such as `REPLACE_SESSION_ID`.
- One repository, one collection file, one PR per coherent API change.
- Do not publish by writing to a local Insomnia app. That means neither the MCP
  write tools (`create_request_in_collection`, `sync_to_insomnia`,
  `update_request`, `set_environment_variable`) nor direct appends to the
  `insomnia.*.db` NeDB store. Read tools (`list_insomnia_collections`,
  `get_insomnia_collection`, `get_environment_variables`) are fine for
  inspecting current local state, and reading the NeDB files is safe while the
  app runs.
- Do not use Insomnia's Import UI to distribute a collection. It lands requests
  in whichever collection is active and cannot be undone.
- The collection is a consumer of the API, not its definition. If the endpoint
  and the collection disagree, the running application wins.
