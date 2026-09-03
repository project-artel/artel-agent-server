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

## Language

Write the pull request title and body in Korean. Keep the Conventional Commit
type and optional scope in English.

Keep verbatim in English regardless: code identifiers, file paths, commands,
log output, error strings, API names, technical terminology, and the section
headings from the body template below.

Which words to reach for is settled by `## Word Choice` in
[`coding-style.md`](coding-style.md), and that rule covers pull request and
issue bodies as much as comments: never invent a Korean word for a technical
meaning, pick the word that is correct rather than the one that sounds
considered, and name the thing and the number instead of reaching for a figure
of speech.

## Title

Use Conventional Commit format, with the summary in Korean:

```text
<type>(<optional-scope>): <한글 변경 사항>
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

## Example

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

## Example

A pull request that changes how data moves, or what a screen shows, carries an
`Example` section. Required for those two kinds of change; omit it for a
documentation, refactor, or configuration change that touches neither.

The point is that a reviewer can see the change happen without running it. A
diff shows what the code became; it does not show what the system now does with
a record.

**Data flow.** Follow one concrete record end to end: the input as it arrives
(wire payload, file, message), what gets written and where — name the tables,
columns, keys, or object paths — and what the next consumer reads back. Prefer
real values taken from a fixture, a test, or a local run over invented ones, and
say which they are. "It is persisted" is not an example; the row is.

**Screens.** Embed a screenshot for every state the change introduces, not just
the successful one. Capture against a running stack whenever the screen can
reach one — a screenshot of mock data proves the component renders, not that the
contract holds. Commit the images into the repository, under
`.plan/assets/<plan-name>/` where the repository keeps plans, and link them with
a `raw.githubusercontent.com` address pinned to the commit hash. A
repository-relative path does not render inside a pull request body, and an
image hosted elsewhere goes blank when that host does.

**Say what the example does not prove.** An example built from seeded rows, a
fixture, or a stubbed dependency demonstrates the shape, not the integration.
State that in the section itself. A reviewer who assumes end-to-end evidence
because none was disclaimed is a reviewer the pull request misled.

## Stacked Pull Requests

When two or more work items in this repository are chained by `blocks`, do not
open their PRs side by side against `develop`. Each PR is based on the one before
it, so a reviewer sees only that link's own diff and nothing needs a rebase after
the PR below it merges.

Use `gh stack`, the `github/gh-stack` extension, instead of setting base branches
by hand:

```bash
gh stack init --base develop <bottom-branch>  # adopt or create the first branch
gh stack add <next-branch>                    # each add sits on top of the previous one
gh stack submit --auto                        # push every branch and open the PRs as drafts
gh stack view --short                         # branches and PR status
gh stack sync                                 # rebase the rest after one below merges
```

- Stack in `blocks` order, bottom first: the item that merges first is the bottom
  of the stack.
- `--auto` opens the PRs as drafts, which is what this repository requires
  anyway. Drop it to write each title and body in the editor instead.
- Every PR in the stack is still a full PR under this document — its own body,
  its own validation, its own `Jira:` trailer. A stack reviews several work items
  in order; it does not merge them into one review.
- Run `gh stack sync` after any PR in the stack merges, and before asking for
  review on the ones above it.
- A `blocks` chain that crosses repositories cannot be stacked, because a stack
  lives in one repository. There the link records merge order and nothing more.

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
