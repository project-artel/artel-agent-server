# Development Workflow

## Why

Small, explicit steps reduce regressions and make review, rollback, and handoff predictable.

## Work Classification

Trivial work:
- documentation typo
- isolated formatting fix
- deterministic one-line configuration change

Non-trivial work:
- behavior change
- bug fix requiring investigation
- dependency or schema change
- cross-module refactor
- user-facing workflow change

Non-trivial work requires a concise plan. Use the `writing-plan` skill when it
is installed.

## End-to-End Flow

1. Confirm goal, scope, acceptance criteria, and non-goals.
2. Read project context, relevant code, tests, and recent changes.
3. Create the Jira issue, the branch, and — once the work is ready — the PR, following `## Jira-Driven Development Flow`. These are part of doing the work; do not wait for an explicit request to create them. Reuse Jira and branch context already provided by the user or environment rather than creating a duplicate.
4. Write a concise implementation plan; use `writing-plan` when installed.
5. Identify architecture impact, tradeoffs, risks, and rollback.
6. Implement the smallest coherent change.
7. Follow `testing.md`; use an installed testing skill when available.
8. Review the complete diff for scope, correctness, and accidental churn.
9. Commit coherent units using the commit convention.
10. Open a draft PR with evidence and explicit remaining risk. Follow
    `pull-request.md` for draft ownership and user-notification rules.
11. Address review without hiding unresolved concerns.

## Jira-Driven Development Flow

Use this pipeline when the work item is tracked in Jira and the user asks for
end-to-end development. Jira access is described in `project.md`.

1. **Create the issue.** `jira_create_issue` in project `ARTEL`, issue type
   `작업` unless the work is an epic or a defect. Follow `issue.md` for the
   body. Set the identifying fields explicitly; a summary alone leaves the
   issue unassigned and unclassified, and it will not show up in the right
   filters:
   - `assignee`: the person who will do the work. Set their Jira `accountId`; never leave it empty or infer ownership from the branch/PR author.
   - `parent`: select the existing Epic that owns this repository and outcome. Every 일반 작업 must have this parent before branch creation.
   - `customfield_10080` (작업 유형): `feat`, `fix`, `chore`, `docs`,
     `refactor`, or `infra`. Required; the call fails without it.
   - `customfield_10081` (레포지토리): `orchestration-server`, `agent-server`,
     `home`, `sdk`, `admin-page`, or `없음`. Required; the call fails without
     it.
   - `labels`: add one only when the work belongs to a theme the two fields
     above do not already express. Reuse an existing label instead of
     inventing a near-duplicate.
   - `customfield_10015` (시작 날짜) and `duedate` (기한): leave them empty at
     creation and stamp them from the commit and PR dates as the work moves —
     see `## Issue Dates`.

   When the deliverable changes more than one repository, this issue is the
   umbrella and each repository gets its own issue — read
   `## Multi-Repository Work` below before setting the fields above.

2. **Move to 진행 중.** Transition the issue. An automation watches this
   transition and creates the branch, so status and branch never drift. The
   generated name is:

   ```text
   <작업 유형>/<issue summary with spaces replaced by hyphens>-<ISSUE KEY>
   ```

   For example, `chore/orchestration-jira-mcp-셋팅-ARTEL-69`. Korean characters
   stay as they appear in the summary, and the branch starts from
   `origin/develop`.

   **The automation is not installed in every repository.** After the
   transition, fetch and confirm the branch exists. When it does not, create it
   manually with the same name — do not invent a different one, and do not
   report the automation as broken before checking.

   The issue key in the branch name is what ties branch, commits, and PR back
   to the issue, so never create the branch before the issue exists. Keep one
   issue per branch, never force-push a shared branch without coordination,
   and delete the branch after merge unless follow-up work depends on it.

3. **Plan.** Use the `writing-plan` skill. Plans land in `.plan/general/`.

4. **Review the plan.** Use the `plan-review` skill.

5. **Loop on the plan.** Fold each finding back into the plan and review again.
   Leave the loop only when no remaining finding requires a plan change. Do not
   start implementing to settle a planning disagreement.

6. **Implement.** Follow the implementation, testing, diff-review, and commit
   steps of `## End-to-End Flow`.

7. **Pair review.** Use the `pair-review` skill, which drives the
   `pair-review-critic` subagent against the implementation. Resolve or
   explicitly accept every finding before opening the PR.

8. **Open the draft PR.** Do this as soon as the work is ready, without waiting to be
   asked. Follow `pull-request.md`, targeting `develop`. Set the assignee and
   the type label, fill in `Code Walkthrough` with one entry per changed unit,
   and end the body with a `Jira: <ISSUE KEY>` trailer so the issue links back.

Move the issue to 검토 중 when the PR opens, and to 완료 only after merge and
required validation pass.

## Issue Dates

Every issue carries 시작 날짜 (`customfield_10015`) and 기한 (`duedate`). Both
record what actually happened in Git rather than an estimate, so the two fields
read as the work's real span:

- **시작 날짜** — the date of the first commit carrying the issue key, or the PR
  open date when that is earlier. Stamp it when the issue moves to 진행 중; if
  the branch has no commit yet, use the date the branch was created.
- **기한** — the date the PR merged. Stamp it when the issue moves to 완료. When
  several PRs carry the key, use the last merge.

Write both as `YYYY-MM-DD` in `Asia/Seoul`, so a late-night commit lands on the
day it was made locally. An umbrella issue takes the earliest 시작 날짜 and the
latest 기한 among its children. Do not overwrite a date that is already set
unless the Git history contradicts it.

## Multi-Repository Work

One deliverable that changes more than one repository is filed as an umbrella
issue plus one issue per repository, linked together. `issue.md` defines the
structure, the link types, and the grouping label; this section covers the
pipeline.

1. **Create the umbrella issue.** Issue type `작업`, 레포지토리 `없음`, parented
   to the Epic that owns the outcome. Record the acceptance criteria for the
   whole deliverable and the merge order — the repository that defines an API,
   schema, or SDK surface merges before the ones that consume it. Give it the
   `xrepo-<slug>` grouping label. The umbrella gets no branch and no PR.

2. **Create one issue per repository.** Same `jira_create_issue` call as step 1
   of `## Jira-Driven Development Flow`, with that repository's 레포지토리
   option, its own 작업 유형, its own assignee accountId, its repository Epic as
   `parent`, and the same `xrepo-<slug>` label. Then link it: `relates to` the
   umbrella, plus `blocks` on the issue whose repository must merge first.

3. **Run steps 2–8 of `## Jira-Driven Development Flow` once per repository
   issue**, in that repository's checkout or worktree, in merge order. A child
   issue is developed exactly like a standalone one — its own branch from the
   automation, plan, plan review, implementation, testing, pair review, and PR,
   with the same 진행 중 / 검토 중 / 완료 transitions. Nothing is skipped because
   the slice is small.

4. **Keep the trail on the child issue.** The branch name, commit trailers, and
   the PR `Jira:` trailer all carry the child issue key, never the umbrella key.

5. **Report up as you go.** Whenever a child changes state, comment the rolled-up
   status on the umbrella: which repositories are merged, which are waiting, and
   any change to the merge order or the shared contract. Move the umbrella to
   완료 last, after every child is merged and validated.

Do not file the children as `Subtask`. That issue type has neither 작업 유형 nor
레포지토리, so its issues fall out of the repository filters and the branch
automation has no prefix to derive a branch name from.

## Change Rules

- Preserve existing architecture unless the task requires changing it.
- Keep unrelated cleanup out of the change.
- Add abstractions only when they remove demonstrated complexity or match an established pattern.
- Keep migrations backward-compatible when practical.
- Prefer reversible rollout for high-risk behavior.

## Stop Conditions

Pause and surface the problem when:
- requirements conflict
- destructive action lacks approval
- required credentials or external access are unavailable
- validation reveals an unrelated pre-existing failure that blocks confidence
- scope expands beyond the agreed issue or plan

Do not silently guess through high-impact ambiguity.
