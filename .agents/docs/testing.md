# Testing Workflow

## Why

Validation depth should match behavior risk and blast radius.

## Strategy

1. Use an installed project testing skill when one applies.
2. Otherwise use verified commands from [`project.md`](project.md).
3. Reproduce current failure or establish baseline.
4. Add or update smallest test at behavior boundary.
5. Run focused tests during implementation.
6. Run broader suite for shared contracts or cross-module changes.
7. Perform manual validation for user-facing behavior when automation is insufficient.

## Risk-Based Coverage

- Low risk: focused unit or static validation.
- Medium risk: affected module tests plus integration boundary.
- High risk: broad regression suite, migration or rollback check, and production-like verification.

## Rules

- Test observable behavior, not private implementation details.
- Testing skill instructions override this general workflow when more specific.
- Do not invent test commands; verify them from project configuration.
- Keep tests deterministic and independent.
- Avoid sleeps, network reliance, and mutable global state unless explicitly controlled.
- Verify failure paths, boundary values, and backward compatibility when relevant.
- Do not claim validation that was not run.

## Reporting

PR and handoff must state:
- commands run
- results
- manual checks
- skipped checks and reason
- known residual risk
