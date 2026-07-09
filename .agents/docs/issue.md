# Issue Workflow

## Why

An issue defines the problem and acceptance boundary. It should not prescribe implementation before investigation.

## Ready Criteria

An issue is ready when it has:
- clear problem statement
- user or system impact
- acceptance criteria
- known constraints
- explicit non-goals when scope could expand
- dependencies or blockers

## Issue Template

```markdown
## Problem

## Impact

## Acceptance Criteria
- [ ]

## Constraints

## Non-goals

## Validation Notes
```

## Lifecycle

1. Create or refine issue.
2. Confirm dependencies and priority.
3. Mark in progress only when active work starts.
4. Link branch, plan, and PR.
5. Update scope changes in issue before implementing them.
6. Close only after acceptance criteria and required validation pass.

## Sizing

Split issue when it contains multiple independently releasable outcomes or requires unrelated ownership areas.

Do not split tightly coupled steps that cannot provide value or validation independently.
