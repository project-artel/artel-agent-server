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

## Assignee and Labels

Every PR carries an assignee and exactly one type label. Set both when opening
the PR rather than leaving them for review time.

- Assignee: the PR author, unless another person owns the merge.
- Label: derived from the Conventional Commit type in the title.

| Title type | Label |
| --- | --- |
| `feat` | `enhancement` |
| `fix` | `bug` |
| `docs` | `documentation` |
| `chore` | `chore` |
| `refactor` | `refactor` |
| `infra` | `infra` |

```bash
gh pr create --draft --assignee @me --label enhancement ...
```

Create the label in the repository when it does not exist yet. Do not
substitute a label that carries a different meaning.

## Body Template

```markdown
## Why

## What Changed

## Code Walkthrough
- `path/to/unit.ext:12` — what the unit now does, and why it had to change

## Validation
- [ ] Command or manual check

## Risks

## Rollback

Jira: ARTEL-123 (omit when no Jira work item exists)
```

`Code Walkthrough` carries one entry per meaningful changed unit — module,
class, function, migration, or configuration file — anchored with `path:line`.
State what the unit now does and why the change was necessary. Do not restate
the diff line by line; the reviewer can read it. Collapse mechanical edits such
as renames or formatting into a single entry.

## Review Rules

- Keep PR focused on one coherent outcome.
- Always create the PR as a draft, even when implementation and validation are
  complete. With GitHub CLI, pass `--draft` to `gh pr create`.
- Agents must never mark a PR ready for review. A human must review the draft
  and manually mark it ready.
- After creating the draft PR, tell the user that human review and manual
  ready-for-review transition are required.
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
