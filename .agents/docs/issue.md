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
- an assignee, a 상위 항목 — the Epic for a standalone `작업`, the `스토리` for a `Subtask` — and the 작업 유형 and 레포지토리 fields set
- an explicit assignee accountId for the person responsible; do not leave ownership to a branch or PR author
- that 상위 항목 chosen before branch creation; do not leave an 일반 작업 or a `Subtask` without a parent

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

A deliverable that changes more than one repository becomes a `스토리` with one
`Subtask` per repository under it. Never track several repositories inside one
issue, and never let a second repository's change ride along in another
repository's issue.

- **`스토리`** — issue type `스토리`, parented to the Epic that owns the outcome. It
  holds the problem, impact, the acceptance criteria for the whole deliverable,
  non-goals, the merge order, and the list of per-repository `Subtask` keys. It
  owns no branch and no PR, and it carries neither 작업 유형 nor 레포지토리 —
  the type has no such field.
- **Per-repository `Subtask`** — one issue type `Subtask` per repository, each with
  the `스토리` as `parent`, its own 작업 유형, its own 레포지토리 option, and its own
  assignee accountId. Each one states the slice of acceptance criteria it
  satisfies and the cross-repository contract it depends on — API path and
  payload, DB column, event name, or SDK version.
- **Links** — the parent is what records membership, so a `Subtask` never links
  `relates to` its `스토리`. Where ordering is real, the `Subtask` that must merge
  first links `blocks` the one that consumes it; the repository that defines an
  API, schema, or SDK surface merges before its consumers. `## Blocks Links`
  below carries the direction and the read-back check.
- The `스토리` closes only after every `Subtask` is merged and validated.

`Subtask` needs four fields the type does not carry by default: 작업 유형
(`customfield_10080`), 레포지토리 (`customfield_10081`), 시작 날짜
(`customfield_10015`), and 기한 (`duedate`). Without 작업 유형 the branch
automation has no prefix to derive a branch name from, and without 레포지토리 the
`Subtask` drops out of every repository filter. When a create call rejects one of
them, stop and ask a Jira admin to put it on the `Subtask` type; do not file the
work as a `작업` to work around it.

Single-repository work stays a single `작업` under its Epic. Do not create a
`스토리` for one repository.

## Blocks Links

`blocks` is the only thing that records merge order, so add one the moment a work
item cannot merge until another does — a caller waiting on an API path, a screen
waiting on a payload field, a consumer waiting on a released SDK surface. Leaving
it out says these may merge in any order, which is a claim rather than a default.

Its direction is easy to get backwards, and a reversed link states the opposite
of what you meant:

```text
jira_create_issue_link(link_type="Blocks",
                       inward_issue_key=<merges first>,
                       outward_issue_key=<waits for it>)
```

Read the link back with `jira_get_issue` and confirm the blocking issue carries
`outward_issue`. The two side names are written from the point of view of the
issue you are reading: `outward_issue` on X means X blocks it, `inward_issue` on
X means X is blocked by it.

- One link per real dependency. Do not chain every issue in a line to impose an
  order nothing requires.
- Add the link when the dependency appears, not at the end. An ordering
  discovered mid-implementation is a link, not a comment.
- Remove the link when the dependency turns out not to exist. A stale `blocks`
  holds up work that could already have merged.

Two issues in the same repository chained by `blocks` get stacked pull requests —
see `## Stacked Pull Requests` in `pull-request.md`.

## Uniform Handling

A per-repository `Subtask` is a full issue, not a checklist line. Every rule in
this document and in `workflow.md` applies to the smallest `Subtask` exactly as
it applies to the `스토리` above it:

- the Ready Criteria above, including an explicit assignee accountId on each
  `Subtask`
- the Issue Template body — a bare summary is not an issue
- its own branch, plan, plan review, implementation, testing, pair review, and PR
- the same transitions: 진행 중 when work starts, 검토 중 when the PR opens,
  완료 after merge and required validation

A one-line change in a second repository still gets its own `Subtask`, branch,
and PR. The `스토리` never absorbs a `Subtask`'s work, and no `Subtask` is
downgraded to a comment because the change looked small.

## Progress Updates

Keep the issue current as the work moves, not at the end. Anyone reading the
issue should see the real state without asking. This applies to a small `Subtask`
as much as to the `스토리` above it.

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
- On a `스토리`, roll the `Subtask` state up as it changes: which repositories are
  merged, which are waiting, and any change to the merge order or the shared
  contract. Add the `blocks` link the moment a new ordering constraint appears.
- Never let the issue contradict git, the PR, or the deploy state. When they
  drift, fix the issue in the same pass.

## Sizing

Split issue when it contains multiple independently releasable outcomes or requires unrelated ownership areas.

Do not split tightly coupled steps that cannot provide value or validation independently.
