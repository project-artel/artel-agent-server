# 2026-07-19 - Agent 기본 인터페이스 추가

- Date: 2026-07-19
- Jira: None
- Status: Draft

## Goal

Add minimal agent interfaces for LLM-backed agents without polluting common request types with scenario-specific draft fields.

## Non-goals

Do not add agent run APIs, persistence, session lifecycle management, tool execution, or final scenario storage.

## Context / Constraints

The first target agent is a chatbot-style test scenario generator. Common agent interfaces should stay generic, while scenario context, draft, and scenario result schemas should live in the scenario agent module.

## Approach (Checklist)
- [x] **Step 0: Recon** (Inspect existing code, locate files)
- [x] **Step 1: Implementation** (Code changes, file paths)
- [x] **Step 2: Tests** (Unit tests, manual verification steps)
- [x] **Step 3: Rollout / Rollback** (Feature flags, migration steps)

## Validation
- **Commands to run:** `python -m pytest`
- **Expected output:** All tests pass.

## Risks & Rollback
- **Risks:** Initial schema shape may need adjustment once the UI flow editor and scenario storage model are defined.
- **Rollback steps:** Revert the agent interface files and tests.

## Open Questions
- None.
