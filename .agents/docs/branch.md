# Branch Workflow

## Why

Predictable branch names expose intent and issue linkage without relying on local context.

## Naming

```text
<type>/<issue-number>
```

Examples:

```text
feat/123
fix/418
docs/527
```

Allowed types:
- `feat`: user-visible capability
- `fix`: defect correction
- `refactor`: behavior-preserving structure change
- `perf`: measured performance improvement
- `test`: test-only change
- `docs`: documentation-only change
- `build`: build or dependency change
- `ci`: automation change
- `chore`: maintenance not covered above

Issue number rules:
- digits only
- must reference the primary issue for the branch
- create or identify the issue before creating the branch

## Lifecycle

- Branch from repository default branch unless project policy says otherwise.
- Keep one primary issue per branch.
- Sync with default branch before final validation when divergence matters.
- Never force-push a shared branch without coordination.
- Delete branch after merge when no follow-up work depends on it.
