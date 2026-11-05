---
name: develop-priority
description: >
  Picks the highest-priority open work item from ./.agents/PRIORITY.md and develops
  it end-to-end using the developer agent (plan → implement → review → PR).
  Invoke when the user says "develop priority", "/develop-priority",
  "developer-priority", "/developer-priority", "develop most priority work item",
  "work on next item", or "next priority".
---

1. Read `./.agents/PRIORITY.md`. Find the first open (no ✓), ready (deps all ✓) work item top-to-bottom.
2. Read `./.agents/handoffs/LATEST.md` for prior context.
3. Read the Jira details recorded in the priority row or linked context. Do not query or create GitHub Issues.
4. Before starting implementation, update the selected work item row in `./.agents/PRIORITY.md` to show it is being developed by the active agent identity:
   - preserve the table structure
   - preserve any existing done marker formatting
   - append ` (in progress by <agent_id>)` to the work item title if no in-progress marker is already present
   - use the CLI program and model name as `<agent_id>`, for example `codex:gpt-5.4`
5. **Scope check**: if the work item has 4+ independent concerns, propose Jira work item splits, add the proposed boundaries to the priority doc, and stop.
6. Invoke `developer` agent with: Jira key or user request, full context, relevant `CLAUDE.md` context, and handoff decisions.

## Rules

- `./.agents/PRIORITY.md` is the canonical source of truth.
- Always include enough work item context when invoking sub-agents.
- Run project-specific validation before creating a PR; tests alone may not verify actual user-facing behavior.
