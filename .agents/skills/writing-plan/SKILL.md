---
name: writing-plan
description: Generate a work plan markdown file under .plan/general/ with strict naming conventions. Use when asked to "write a plan" or when starting any non-trivial change. Optionally links plans to Jira.
---

# writing-plan Skill

## Trigger conditions
Use this skill when:
- The user asks for a plan / planning / 작업 플랜 / 실행 계획.
- You are about to implement a non-trivial change and there is no plan file yet.
- You are preparing a PR and need a PR-reviewable plan.

## Inputs (ask if missing)
- **title** (required): Short human-readable title for the plan.
- **jira** (optional): Jira key or work item URL.

## Output rules (MUST)

- **Date format:** `YYYY-MM-DD` (must appear in filename and inside file body).
- **Slug generation:**
  - **Translate non-English titles to English summary for the slug.** (e.g., "가격 캐시 추가" -> "add-price-cache")
  - Lowercase everything.
  - Replace spaces/underscores with `-`.
  - Remove special characters.
  - Collapse repeated `-` and trim leading/trailing `-`.
  - Keep reasonably short (<= 50 chars).

### Plan file
1. **Ensure directory:** `.plan/general/`
2. **Check collision:**
   - Target: `.plan/general/<YYYY-MM-DD>-<slug>.md`
   - If target exists, append suffix: `-2`, `-3`, etc. (e.g., `...-slug-2.md`)
3. **Create File:** `.plan/general/<YYYY-MM-DD>-<slug>.md`

4. **Jira linkage:** Jira metadata is optional and does not affect the file path. Do not create or require a GitHub Issue.

## Plan content (MUST)
1. **Load template:** Read `assets/PLAN_TEMPLATE.md`.
2. **Replace placeholders:**
   - `{{DATE}}` → Current date (YYYY-MM-DD)
   - `{{TITLE}}` → User provided title (Keep original language here)
   - `{{JIRA_OR_NONE}}` → Jira key or URL, or string "None"
3. **Fill sections:** Do not delete headers. Add initial thoughts if context is available.

## Return format (in chat)
After creating the plan file, respond with:
- **Created file path** (clickable if possible)
- **3–5 bullet summary** of the plan goal
- **Open questions** (if any context is missing)
