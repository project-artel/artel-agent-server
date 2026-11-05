# 2026-07-16 - Jenkins와 Docker 배포 설정 추가

- Date: 2026-07-16
- Jira: None
- Status: Draft

## Goal

Add Jenkins and Docker configuration so the FastAPI app can be built into a container and deployed separately to stage or operation based on branch name, with runtime configuration loaded from mounted environment files.

## Non-goals

Do not change application runtime behavior, add agent APIs, or configure Jenkins credentials/secrets inside the repository.

## Context / Constraints

Follow the deployment shape from the existing orchestration server Jenkinsfile: branch-based environment resolution, Docker image tagging, container replacement, and `app-net` network usage. This repository is Python/FastAPI, so validation should use `pytest` and the container should run `uvicorn`.

## Approach (Checklist)
- [x] **Step 0: Recon** (Inspect existing code, locate files)
- [x] **Step 1: Implementation** (Code changes, file paths)
- [x] **Step 2: Tests** (Unit tests, manual verification steps)
- [x] **Step 3: Rollout / Rollback** (Feature flags, migration steps)

## Validation
- **Commands to run:** `python -m pytest`; `Get-Command docker`
- **Expected output:** All tests pass. Docker is available on Jenkins; local Docker command was not available in this workspace.

## Risks & Rollback
- **Risks:** Jenkins host must provide Docker, Python, `app-net`, and environment-specific `.env.stage` / `.env.operation` files.
- **Rollback steps:** Revert the Jenkinsfile/Dockerfile commit and redeploy the previous container image.

## Open Questions
- None.
