# Pull Request Workflow

## Why

PR should let reviewer understand intent, verify evidence, and identify risk without reconstructing development history.

## Before Opening

- Confirm acceptance criteria from Jira or the user request.
- Update plan to reflect final implementation.
- Review full diff against default branch.
- Remove debug code and unrelated churn.
- Run required validation.
- Confirm migrations, configuration, and rollback needs.

## Title

Use Conventional Commit format:

```text
<type>(<optional-scope>): <imperative summary>
```

## Body Template

```markdown
## Why

## What Changed

## Validation
- [ ] Command or manual check

## Risks

## Rollback

Jira: ARTEL-123 (omit when no Jira work item exists)
```

## Review Rules

- Keep PR focused on one coherent outcome.
- Mark draft while known required work remains.
- Respond to each actionable review comment.
- Resolve threads only after change or explicit agreement.
- Add new commits during review when history clarity matters.
- Squash only when repository policy prefers a single final commit.

## Merge Criteria

- acceptance criteria satisfied
- required checks pass
- review approvals complete
- unresolved risks explicitly accepted
- deployment or migration order documented
