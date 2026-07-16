# 2026-07-16 - FAST API 스켈레톤 구축

- Date: 2026-07-16
- Jira: None
- Status: Draft

## Goal

Create a minimal Python FastAPI backend skeleton for the AI agent operation server, including app initialization, health endpoint, initial LLM client abstraction, OpenRouter client shell, configuration, and focused tests.

## Non-goals

Do not implement agent run APIs, agent managers, persistent storage, background workers, or real agent reasoning in this first skeleton.

## Context / Constraints

The repository is currently almost empty aside from agent instructions. Keep the first implementation small, explicit, and focused on the HTTP app shell plus the LLM boundary that later Agent, Tool, and Reasoning layers can use.

## Approach (Checklist)
- [x] **Step 0: Recon** (Inspect existing code, locate files)
- [x] **Step 1: Implementation** (Code changes, file paths)
- [x] **Step 2: Tests** (Unit tests, manual verification steps)
- [x] **Step 3: Rollout / Rollback** (Feature flags, migration steps)

## Validation
- **Commands to run:** `python -m pytest`; `python -m compileall app tests`
- **Expected output:** All tests pass and files compile.

## Risks & Rollback
- **Risks:** Initial dependency choices may need adjustment when persistence, worker queues, or OpenRouter integration are added.
- **Rollback steps:** Revert this skeleton commit or remove the added `app/`, `tests/`, and packaging files.

## Open Questions
- Preferred dependency manager is not yet defined.
- Production deployment target is not yet defined.
