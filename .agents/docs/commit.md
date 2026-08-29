# Commit Workflow

## Why

Each commit should explain one coherent change and remain safe to review or revert independently.

## Format

Write the commit title and body in Korean. Keep the Conventional Commit type
and optional scope in English.

Keep verbatim in English regardless: code identifiers, file paths, commands,
log output, error strings, API names, and technical terminology.

Which words to reach for is settled by `## Word Choice` in
[`coding-style.md`](coding-style.md), and that rule covers commit text as much
as comments: never invent a Korean word for a technical meaning, pick the word
that is correct rather than the one that sounds considered, and name the thing
and the number instead of reaching for a figure of speech.

Use Conventional Commits:

```text
<type>: <한글 변경 사항>
```

Examples:

```text
feat: 세션 만료 기능 추가
refactor: 인증 책임 분리
chore: 개발 환경 설정 정리
docs: 로컬 테스트 방법 문서화
fix: 빈 응답 처리 오류 수정
```

Allowed types:
- `feat`: feature or user-visible behavior change
- `refactor`: behavior-preserving structural change
- `chore`: maintenance outside product behavior
- `docs`: documentation-only change
- `fix`: defect correction

## Rules

- Keep subject at 50 characters or fewer when practical.
- Write the change summary after `<type>: ` in Korean.
- Do not end subject with a period.
- Describe why in body when motivation is not obvious.
- Reference Jira in footer only when repository automation requires it.
- Do not mix unrelated behavior, formatting, and refactoring.
- Do not commit secrets, generated noise, or local-only configuration.

## Body

Add a body when change has non-obvious constraints or tradeoffs:

```text
fix: 새로고침 중 기존 캐시 값 유지

Concurrent refreshes previously cleared readable values. Keep stale data
until replacement succeeds so callers retain deterministic fallback behavior.

Jira: ARTEL-123
```
