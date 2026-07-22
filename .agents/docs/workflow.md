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
3. Use Jira and branch context provided by the user or environment. Do not create issues or branches unless explicitly requested; when they are, follow `## Jira-Driven Development Flow`.
4. Write a concise implementation plan; use `writing-plan` when installed.
5. Identify architecture impact, tradeoffs, risks, and rollback.
6. Implement the smallest coherent change.
7. Follow `testing.md`; use an installed testing skill when available.
8. Review the complete diff for scope, correctness, and accidental churn.
9. Commit coherent units using the commit convention.
10. Open a PR with evidence and explicit remaining risk.
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
     `home`, `sdk`, or `없음`. Required; the call fails without it.
   - `labels`: add one only when the work belongs to a theme the two fields
     above do not already express. Reuse an existing label instead of
     inventing a near-duplicate.

2. **Move to 진행 중 and create the branch.** Transition the issue, then create
   the branch in the same step so status and branch never drift. Derive the
   name from the issue:

   ```text
   <작업 유형>/<issue summary with spaces replaced by hyphens>-<ISSUE KEY>
   ```

   For example, `chore/orchestration-jira-mcp-셋팅-ARTEL-69`. Keep Korean
   characters as they appear in the summary. Branch from `origin/develop`.

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

8. **Open the PR.** Follow `pull-request.md`, targeting `develop`. Fill in
   `Code Walkthrough` with one entry per changed unit, and end the body with a
   `Jira: <ISSUE KEY>` trailer so the issue links back.

Move the issue to 검토 중 when the PR opens, and to 완료 only after merge and
required validation pass.

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
