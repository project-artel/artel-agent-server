---
name: insomnia-sync
description: >
  Analyzes the current repository's API surface and publishes it as an Insomnia
  collection to the shared `project-artel/insomnia-api` repository through a
  commit/push/PR — including environment variables. Replaces the older approach
  of registering collections directly into the local Insomnia app over MCP.
  Invoke when the user says "insomnia sync", "/insomnia-sync", "insomnia 반영",
  "API 인섬니아에 올려", "컬렉션 갱신", or "insomnia 컬렉션 PR".
---

Insomnia collections are source-controlled in `project-artel/insomnia-api`. Each
repository owns exactly one collection file, and every consumer receives it
through Insomnia's git sync. Changes reach people by merging a PR, never by
writing into someone's local Insomnia app.

## Steps

1. **Identify the collection file.** It is `<repo-name>.yaml` at the root of
   `project-artel/insomnia-api` — `artel-agent-server` → `agent-server.yaml`.
   Drop a leading `artel-` from the repository name. If the file does not exist
   yet, this run creates it, starting from `assets/COLLECTION_TEMPLATE.yaml` and
   deleting the request shapes the service does not have.

2. **Derive the API surface from the repository, not from memory.** Prefer the
   generated contract over source reading:
   - FastAPI: run the app and fetch `/openapi.json`, or import
     `app.main:create_app` and call `app.openapi()`.
   - Otherwise, enumerate route declarations (`@router.*`, `@RequestMapping`,
     `router.<verb>(`) and their request/response models.

   For each endpoint capture: method, path, tag, summary/description, and a
   realistic example request body built from the actual schema — required
   fields, enum values spelled out, no invented properties.

3. **Clone the collection repository into the scratchpad** and branch from the
   default branch:

   ```bash
   git clone https://github.com/project-artel/insomnia-api.git
   cd insomnia-api && git checkout -b feat/<repo-slug>-<change>
   ```

   Never edit Insomnia's own clone under
   `%APPDATA%\Insomnia\version-control\git\`. That directory is the app's
   working copy; the app owns its state.

4. **Write the collection file.** What binds is Insomnia's own schema 5.1 — the
   top-level keys, the `wrk_`/`req_`/`env_` id prefixes, epoch-millisecond
   timestamps — because the app refuses to parse anything else.
   `assets/COLLECTION_TEMPLATE.yaml` is one filled-in example of that schema,
   lifted from `agent-server.yaml`; consult it for the shape of a construct you
   are unsure how to express, not as a layout to reproduce. Its endpoints,
   `sortKey` values, and request mix are illustrative.

   Reconcile rather than regenerate: keep existing `meta.id` values for requests
   that still exist, so history stays reviewable and local response references
   survive. Add, update, and delete only what the API surface actually changed.

5. **Define environments in the same file.** Every URL must resolve without
   manual editing after a pull:
   - `environments.data` — shared defaults, pointing at staging.
   - `environments.subEnvironments` — one entry per deployment target
     (`local`, and `prod` where one exists), overriding the same variable names.
   - Variables are `stage_<service>_base_url` and `stage_<service>_ws_url`,
     following `agent-server.yaml`'s `stage_agent_*` and the `stage_orch_*` used
     for orchestration. The `stage_` prefix names the Base Environment default,
     not the only target — sub-environments override the same keys, so a `local`
     run still reads `stage_<service>_base_url`. That reads oddly; leave it
     until the team renames it across every collection at once.
   - Match the HTTP scheme to the WS scheme: `https` pairs with `wss`, `http`
     with `ws`.

6. **Validate before committing:**

   ```bash
   python -c "import yaml,sys; d=yaml.safe_load(open(sys.argv[1],encoding='utf-8')); print(len(d['collection']),'requests')" <file>.yaml
   ```

   Then confirm every `{{ _.name }}` in the file resolves against a defined
   variable:

   ```bash
   grep -oE '\{\{ *_\.[a-zA-Z0-9_]+ *\}\}' <file>.yaml | sort -u
   ```

   An unresolved variable renders as an empty string and the request silently
   fails — this is the failure mode the skill exists to prevent.

7. **Commit, push, and open a PR** against the default branch, following
   `.agents/docs/commit.md` and `.agents/docs/pull-request.md`. The PR body
   states which endpoints were added, changed, or removed, and which source
   commit of the origin repository the collection reflects.

8. **Report the PR URL.** Do not merge. After merge, consumers pull in Insomnia
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
- Do not use the Insomnia MCP write tools (`create_request_in_collection`,
  `sync_to_insomnia`, `update_request`, `set_environment_variable`) to publish.
  They mutate one machine's local app and produce no reviewable diff. Read tools
  (`list_insomnia_collections`, `get_insomnia_collection`,
  `get_environment_variables`) are fine for inspecting current local state.
- The collection is a consumer of the API, not its definition. If the endpoint
  and the collection disagree, the running application wins.
