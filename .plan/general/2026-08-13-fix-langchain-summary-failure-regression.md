# 2026-08-13 — langchain 1.3.15 요약 실패 처리 회귀 수정

- Date: 2026-08-13
- Jira: ARTEL-365
- Status: Ready

## Goal

요약 모델 호출이 실패해도 QA 런이 대화 이력을 잃지 않고 계속되도록, langchain
1.3.14와 1.3.15 양쪽에서 동일하게 동작하게 만든다. 그 결과로 CI의
`docker build --target test`가 다시 통과한다.

## Non-goals

- `Dockerfile`이 `uv.lock`을 쓰도록 바꾸는 것. 드리프트의 근원이지만 별건이다
- 압축 트리거·보존 정책·요약 프롬프트 변경
- langchain 메이저 상한(`<2`) 조정, 또는 `<1.3.15`로 낮추는 회피

## Context / Constraints

`SummarizationMiddleware`가 1.3.15에서 두 가지를 바꿨다.

1. `__init__`이 `self.model.with_retry()`를 호출한다 — 생성자에 넘기는 모델이
   `Runnable`이어야 한다.
2. `_create_summary` / `_acreate_summary`의 `try/except`가 사라졌다. 예전에는
   실패를 `"Error generating summary: ..."` 문자열로 돌려줬고, 이제는 재시도
   3회 후 예외를 그대로 올린다.

`app/agents/qa/compaction.py`의 `abefore_model`은 그 문자열을
`_SUMMARY_FAILURE_PREFIXES`로 검사해 압축 업데이트 전체를 거절하는 방식으로
이력 유실을 막고 있었다. 1.3.15에서는 문자열이 오지 않으므로 가드가 죽고,
예외가 미들웨어 밖으로 튀어 런이 중단된다.

제약:

- `SummarizationMiddleware` 상속 구조는 유지한다. 도구 호출과 그 결과를 갈라
  자르지 않는 부분이 이미 그 안에서 검증되어 있다
- 두 버전 모두 지원해야 한다. 로컬 `.venv`는 1.3.14, CI는 1.3.15다

## Approach (Checklist)

- [ ] **Step 0: Recon** — 두 버전의 `summarization.py` 차이 확인(완료),
      `object()`를 모델로 넘기는 테스트 스텁 위치 확인(완료:
      `tests/test_qa_model_reasoning.py:307`, `tests/test_qa_prompt_version.py:149`)
- [ ] **Step 1: Implementation**
  - `app/agents/qa/compaction.py`: `abefore_model`에서
    `super().abefore_model(...)`을 `except Exception`으로 감싸, 기존 실패 경로와
    같은 곳(오퍼레이터 SYSTEM 노트 + `return None`)으로 보낸다. 예외 내용은
    모듈 로거로 남긴다 — 삼키기만 하면 진단이 사라진다
  - `fold_stale_scenes` 호출은 `try` **밖으로** 뺀다. 지금처럼 인자 자리에서
    평가되면 우리 쪽 폴딩 버그까지 요약 실패로 둔갑해 조용히 삼켜진다
  - 노트 발신을 private 메서드로 뽑아 문자열 프리픽스 경로와 예외 경로가 같은
    문구를 쓰게 한다
  - 기존 프리픽스 가드는 그대로 둔다. 1.3.14에서는 여전히 그 경로로 온다
- [ ] **Step 2: Tests**
  - `tests/test_qa_model_reasoning.py`, `tests/test_qa_prompt_version.py`:
    `build_chat_model` 스텁이 `object()` 대신 `with_retry(*args, **kwargs)`가
    자기 자신을 돌려주는 최소 스텁을 반환하게 한다. 인자를 받아두는 것은 이
    호출의 시그니처가 우리 것이 아니기 때문이다
  - `tests/test_qa_compaction.py`: 요약이 레거시 실패 문자열로 돌아오는 경로를
    검증하는 테스트를 추가한다. 기존 예외 테스트는 1.3.15에서만 예외 경로를
    타므로, 프리픽스 가드는 별도 테스트가 없으면 버전에 따라 검증되지 않는다.
    두 테스트 모두 `state.compactions == 0`과 SYSTEM 노트를 함께 확인해,
    "거절했다"가 아니라 "이력이 남고 오퍼레이터가 안다"를 검증하게 한다
- [ ] **Step 3: Rollout / Rollback** — 런타임 플래그·마이그레이션 없음.
      `git revert` 한 번으로 되돌아간다

## Validation

- **Commands to run:**
  - `docker build --target test .` (CI와 동일한 환경, langchain 1.3.15)
  - `.venv/bin/python -m pytest` (메인 체크아웃, langchain 1.3.14 하위 호환)
- **Expected output:** 양쪽 모두 전체 통과. 도커 빌드는 `Test` 타깃에서
  실패하지 않는다

## Risks & Rollback

- **Risks:**
  - `except Exception`이 넓다. 요약 실패 외의 프로그래밍 오류까지 삼켜 압축이
    조용히 멈출 수 있다 — 로거로 남기는 이유다. 좁히려면 어떤 예외가 오는지가
    provider별로 달라져 오히려 새는 구멍이 생긴다
  - 1.3.15의 재시도(3회)로 요약 실패 시 지연이 늘어난다. 압축은 이미 모델
    호출 경로이므로 런 전체가 멈추는 것보다는 낫다
- **Rollback steps:** `git revert`

## Rejected feedback

- `except Exception`을 provider별 예외 타입으로 좁히자 — 거절. 요약 모델은
  설정으로 갈아끼우는 자리라 어떤 예외가 오는지가 provider마다 다르다. 좁히면
  구멍이 새고, 새는 쪽 결과가 바로 "이력 전체 유실"이다
- 노트 문구를 예외 경로에서 다르게 쓰자 — 거절. 오퍼레이터에게는 같은 사건이다.
  구분이 필요한 쪽은 로그다

## Open Questions

- 없음
