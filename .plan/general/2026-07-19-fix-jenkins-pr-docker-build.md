# 2026-07-19 - Jenkins PR 감지와 Docker 기반 빌드 수정

- Date: 2026-07-19
- Jira: None
- Status: Draft

## Goal

Update Jenkins and Docker configuration so pull request builds are handled safely and tests/builds run through Docker rather than relying on Python installed on the Jenkins host.

## Non-goals

Do not configure Jenkins webhooks, Jenkins job settings, credentials, or production secrets from the repository.

## Context / Constraints

Jenkinsfile can detect PR builds when Jenkins provides multibranch variables such as `CHANGE_ID` and `CHANGE_TARGET`, but the Jenkins job itself must be configured to discover PRs and receive GitHub webhook events. The Jenkins host is assumed to have Docker available.

## Approach (Checklist)
- [x] **Step 0: Recon** (Inspect existing code, locate files)
- [x] **Step 1: Implementation** (Code changes, file paths)
- [x] **Step 2: Tests** (Unit tests, manual verification steps)
- [x] **Step 3: Rollout / Rollback** (Feature flags, migration steps)

## Validation
- **Commands to run:** `python -m pytest`; Docker build validation if Docker is available
- **Expected output:** Tests pass; Jenkins can run Dockerfile `test` target and `runtime` target.

## Risks & Rollback
- **Risks:** Jenkins PR detection still requires the job to be configured as a multibranch pipeline or GitHub Branch Source job with PR discovery enabled.
- **Rollback steps:** Revert this Jenkinsfile/Dockerfile change and restore the previous pipeline.

## Open Questions
- Jenkins job type and GitHub webhook/branch source configuration should be checked in Jenkins UI.
