# 2026-08-11 — agent-server 피처 브랜치 Jenkins 빌드 실패 수정

- Date: 2026-08-11
- Jira: ARTEL-300 (Epic ARTEL-14 [Infra] 개발·운영 인프라)
- Branch: `infra/agent-server-피처-브랜치-jenkins-빌드-실패-수정-ARTEL-300`
- Status: Draft

## Goal

피처 브랜치의 Jenkins 브랜치 job이 `Test`까지 도달해 SUCCESS로 끝나게 한다. 배포 경로는 그대로 둔다.

## Non-goals

- 배포 전략, 이미지 태깅 정책, 컨테이너 런타임 옵션 변경
- Jenkins 인스턴스 설정(job 디스커버리, credential) 변경
- PR job 동작 변경. 이미 통과하고 있다
- 다른 레포로 확장. orchestration은 [ARTEL-166](https://artel-asm.atlassian.net/browse/ARTEL-166)에서 이미 고쳐졌다

## Context / Constraints

`Resolve Target` 스테이지가 브랜치 빌드에서 `resolveTargetEnv(env.BRANCH_NAME)`를 무조건 부른다. 이 함수는 `main`/`operation`/`develop`/`stage`가 아닌 이름에 `error`를 던지므로 피처 브랜치 job은 `Test`에 도달하지도 못하고 죽는다.

PR job은 `env.CHANGE_ID`가 있어 `IS_PR='true'` 분기를 타고 `resolveTargetEnv`를 부르지 않는다. 그래서 실제 검증 신호는 살아 있었고, 죽은 것은 브랜치 job 하나뿐이다.

**해법은 이미 검증돼 있다.** [ARTEL-166](https://artel-asm.atlassian.net/browse/ARTEL-166)이 orchestration-server에서 같은 원인을 고쳤다. 배포 스테이지 전체를 `when { anyOf { branch ... } }`로 감싸고 `resolveTargetEnv` 호출을 그 안으로 옮기는 구조다. 이 작업은 그 구조를 agent-server로 옮긴다 — 두 레포의 파이프라인이 갈리면 다음 사람이 두 번 배운다.

agent-server 쪽에는 orchestration에 없는 제약이 하나 있다. `Test` 스테이지가 모든 빌드에서 돌고 `TEST_IMAGE_TAG`를 요구하는데, 기존에는 그 태그를 `TARGET_ENV`로 지었다. 배포 대상이 아닌 브랜치에는 `TARGET_ENV`가 없으므로 다른 이름이 필요하다.

**태그를 브랜치 이름으로 지을 수 없다.** 브랜치 이름에 `/`와 한글이 들어가고 docker 태그는 `[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}`만 받는다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 완료
  - `Jenkinsfile`의 `resolveTargetEnv`가 실패 지점
  - orchestration `Jenkinsfile:83`의 `Deploy Pipeline` 구조가 옮길 패턴
  - PR #66/#63/#62에서 브랜치 job만 실패하고 pr-head는 통과하는 것을 실측 확인

- [x] **Step 1: `Resolve Target`을 테스트 태그만 정하게 축소**
  - PR이면 기존대로 `pr-<CHANGE_ID>-<BUILD_NUMBER>-test`
  - 아니면 `branch-<BUILD_NUMBER>-test`
  - **브랜치 이름을 태그에 넣지 않는다.** 위 제약 때문이다. 빌드 번호가 브랜치를 대신하므로 두 브랜치 job이 같은 태그로 충돌할 수 있는데, 대가가 없다 — 이 이미지는 `docker build --target test`가 만든 일회용 라벨이고 실제 산출물은 그 안에서 돈 pytest의 종료 상태다. 태그는 이후 아무 데서도 쓰이지 않는다

- [x] **Step 2: `Docker Build`와 `Deploy`를 `Deploy Pipeline`으로 묶기**
  - `when { anyOf { branch 'main'; branch 'operation'; branch 'develop'; branch 'stage' } }`
  - `resolveTargetEnv` 호출과 `TARGET_ENV`·`CONTAINER_NAME`·`IMAGE_TAG`·`ENV_FILE` 설정을 `Docker Build` 안으로 이동
  - 게이트를 `expression { env.IS_PR != 'true' }`에서 브랜치 이름 기준으로 바꾸는 것이 핵심이다. 기존 게이트는 "PR이 아니다"만 봤고, 피처 브랜치는 PR이 아니므로 통과해 버렸다

## Validation

- **정적 검증 (수행함):**
  - 중괄호 균형 — 문자열·주석을 건너뛰고 계산해 OK
  - 선언적 문법: `Deploy Pipeline`의 직속 자식이 `when`과 `stages`뿐이고 `steps`가 없음. 한 stage가 `steps`와 `stages`를 함께 가지면 파싱이 거부된다
  - 배포 변수 설정·사용 지점 대조: `TARGET_ENV`·`CONTAINER_NAME`·`IMAGE_TAG`·`ENV_FILE` 모두 `Docker Build`에서 설정되고 `Deploy`에서 쓰인다. 같은 부모 안이라 순서가 보장된다
- **검증하지 못한 것:**
  - **Jenkins 선언적 린터를 돌리지 못했다.** `jenkins.artel.kr`이 인증을 요구하고(HTTP 403) 자격증명이 없다. 로컬에 Groovy도 없다. 문법 오류가 남아 있다면 이 브랜치의 첫 빌드에서 드러난다
  - **배포 경로 실행.** `develop`/`main`에서만 도는 경로라 이 브랜치에서는 실행되지 않는다. 병합 후 `develop` 빌드가 기존대로 도는지 확인이 필요하다
- **머지 후 확인할 것:**
  - 이 브랜치 job이 `Test`까지 돌고 SUCCESS인지
  - PR job이 여전히 SUCCESS인지
  - 콘솔 로그에 `Unsupported branch for deployment`가 없는지

## Risks & Rollback

- **Risks:**
  - **배포 경로 회귀의 대가가 크다.** `develop` 병합 시 실제 스테이지 배포가 걸린다. 변경은 스테이지를 옮기고 감싼 것이지 배포 명령 자체를 건드리지 않았지만, 실행으로 확인되지 않았다는 사실은 남는다
  - **린터 미실행.** 문법 오류가 있으면 브랜치 첫 빌드에서 실패한다. 다만 그 실패는 지금 상태(항상 실패)보다 나쁘지 않다
  - **`IS_PR`의 소비자가 줄었다.** 이제 `Resolve Target` 내부 분기에서만 쓰인다. 남겨둔 `TARGET_BRANCH`도 이 파일에서 읽는 곳이 없다 — 기존부터 그랬고, 이 이슈 범위가 아니라 건드리지 않았다

- **Rollback steps:** `git revert`. 파일 하나이고 상태나 마이그레이션이 없다

## Open Questions

- `TARGET_BRANCH`는 설정만 되고 읽히지 않는다. 죽은 변수로 보이나 이 이슈 범위 밖이라 두었다. 별건으로 정리할지
