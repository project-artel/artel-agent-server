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
- an assignee, an existing Epic (상위 항목), and the 작업 유형 and 레포지토리 fields set
- an explicit assignee accountId for the person responsible; do not leave ownership to a branch or PR author
- an Epic selected from the repository and work domain before branch creation; do not leave an 일반 작업 without a parent

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
3. Mark in progress only when active work starts. In repositories wired for it,
   this transition triggers the branch automation described in `workflow.md`;
   confirm the branch appeared, and create it manually when it did not.
4. Link branch, plan, and PR. The branch name carries the issue key, which is
   what ties commits and the PR back to this issue.
5. Update scope changes in issue before implementing them.
6. Close only after acceptance criteria and required validation pass.

## Multi-Repository Work

A deliverable that changes more than one repository becomes an umbrella issue
plus one issue per repository, tied together with links. Never track several
repositories inside one issue, and never let a second repository's change ride
along in another repository's issue.

- **Umbrella issue** — issue type `작업`, parented to the Epic that owns the
  outcome, 레포지토리 `없음`, 작업 유형 set to the dominant type of the
  deliverable. It holds the problem, impact, the acceptance criteria for the
  whole deliverable, non-goals, the merge order, and the list of per-repository
  issue keys. It owns no branch and no PR.
- **Per-repository issue** — one issue type `작업` per repository, each with its
  own 작업 유형, its own 레포지토리 option, its own assignee accountId, and the
  Epic that owns that repository as `parent`. Each one states the slice of
  acceptance criteria it satisfies and the cross-repository contract it depends
  on — API path and payload, DB column, event name, or SDK version.
- **Links** — every per-repository issue links `relates to` the umbrella issue.
  Where ordering is real, the issue that must merge first links `blocks` the one
  that consumes it; the repository that defines an API, schema, or SDK surface
  merges before its consumers.
- **Grouping label** — put the same `xrepo-<slug>` label on the umbrella and
  every child, with `<slug>` a short kebab-case name for the deliverable, for
  example `xrepo-issue-read-api`. This is the only label that may be coined per
  deliverable; it is what makes the whole set retrievable in one JQL query.
- The umbrella closes only after every child issue is merged and validated.

Do not use `Subtask` for this. That issue type exposes neither 작업 유형 nor
레포지토리, so its children drop out of every repository filter and the branch
automation cannot derive a branch name for them.

Single-repository work stays a single 작업. Do not create an umbrella for one
repository.

## Uniform Handling

A per-repository child issue is a full issue, not a checklist line. Every rule in
this document and in `workflow.md` applies to the smallest child exactly as it
applies to the largest umbrella:

- the Ready Criteria above, including an explicit assignee accountId on each
  child
- the Issue Template body — a bare summary is not an issue
- its own branch, plan, plan review, implementation, testing, pair review, and PR
- the same transitions: 진행 중 when work starts, 검토 중 when the PR opens,
  완료 after merge and required validation

A one-line change in a second repository still gets its own issue, branch, and
PR. The umbrella never absorbs a child's work, and no child is downgraded to a
comment because the change looked small.

## Progress Updates

Keep the issue current as the work moves, not at the end. Anyone reading the
issue should see the real state without asking. This applies to a small child
issue as much as to an umbrella.

- Transition status the moment reality changes: 진행 중 when work actually
  starts, 검토 중 when the PR opens, 완료 after merge and required validation.
  Never batch transitions at the end of the work.
- Comment at each milestone, with links rather than prose: plan path, branch
  name, PR URL, validation or test evidence, and the decision taken whenever the
  approach changed.
- Comment as soon as the work is blocked, naming what blocks it and what would
  unblock it. Do not leave a stalled issue sitting in 진행 중 with no
  explanation.
- Edit the description when scope, acceptance criteria, or non-goals change, and
  do it before implementing the change. Comments record history; the description
  records the current contract.
- On an umbrella, roll the child state up as it changes: which repositories are
  merged, which are waiting, and any change to the merge order or the shared
  contract. Add the `blocks` link the moment a new ordering constraint appears.
- Never let the issue contradict git, the PR, or the deploy state. When they
  drift, fix the issue in the same pass.

## Sizing

Split issue when it contains multiple independently releasable outcomes or requires unrelated ownership areas.

Do not split tightly coupled steps that cannot provide value or validation independently.
