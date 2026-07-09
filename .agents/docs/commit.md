# Commit Workflow

## Why

Each commit should explain one coherent change and remain safe to review or revert independently.

## Format

Use Conventional Commits:

```text
<type>(<optional-scope>): <imperative summary>
```

Examples:

```text
feat(auth): add session timeout
fix(api): handle empty upstream response
docs: document local test setup
```

Use the same types defined in [`branch.md`](branch.md).

## Rules

- Keep subject at 50 characters or fewer when practical.
- Use imperative present tense.
- Do not end subject with a period.
- Describe why in body when motivation is not obvious.
- Reference issue in footer when repository automation requires it.
- Do not mix unrelated behavior, formatting, and refactoring.
- Do not commit secrets, generated noise, or local-only configuration.

## Body

Add a body when change has non-obvious constraints or tradeoffs:

```text
fix(cache): preserve stale values during refresh

Concurrent refreshes previously cleared readable values. Keep stale data
until replacement succeeds so callers retain deterministic fallback behavior.

Refs #123
```
