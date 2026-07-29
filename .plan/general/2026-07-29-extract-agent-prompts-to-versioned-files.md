# 2026-07-29 — 2차 스프린트: Agent 프롬프트를 파일로 분리하고 버전으로 고르게 한다

- Date: 2026-07-29
- Jira: ARTEL-179
- Status: Draft

## Goal

살아있는 프롬프트 3종(QA 실행 루프, scenario, game_context)을
`app/prompts/<agent>/<version>/<role>.md`로 옮기고, 버전을 `명시 인자 > 환경 설정
기본값 > 디렉터리 내 최신` 순으로 고르게 한다. 실제 사용한 버전이 런 시작 로그에
남고, 잘못된 프롬프트는 기동 시점에 프로세스를 죽인다. 함께 죽은 프롬프트 경로
(`QaExecutionAgent`, `ACT_*`/`EVAL_*`/`CHAT_*`, 딸린 스키마와 export)를 지운다.

## Non-goals

- 프롬프트 문구 개선. v1은 현재 상수를 바이트 단위로 그대로 옮긴 것이다.
- 프롬프트 조회/편집 API, 런타임 핫 리로드.
- `tools.py`의 툴 description 파일화.
- `OUTPUT_CONTRACT` dict과 `LANGUAGE_DIRECTIVES`의 파일화. pydantic 스키마·enum과
  짝이므로 파일로 빼면 조용히 어긋난다.

## Context / Constraints

- 옮길 상수
  - `app/agents/qa/runner.py` `SYSTEM_PROMPT` → `app/prompts/qa_run/v1/system.md`
  - `app/agents/scenario/prompt.py` `SYSTEM_PROMPT` / `HUMAN_TEMPLATE`
    → `app/prompts/scenario/v1/{system,human}.md`
  - `app/agents/game_context/prompt.py` `SYSTEM_PROMPT` / `HUMAN_TEMPLATE`
    → `app/prompts/game_context/v1/{system,human}.md`
- 파일 형식은 frontmatter(`version`, `note`, `placeholders`) + 본문. YAML 의존성이
  `pyproject.toml`에 없고 추가하지 않는다. frontmatter는 stdlib만으로 파싱할 수
  있는 최소 문법(스칼라 문자열, 인라인 리스트)으로 제한한다.
- 상수는 인접 문자열 리터럴로 조립돼 있다. 이어붙인 결과와 파일 본문이 바이트 단위로
  같아야 한다. 줄바꿈/공백을 임의로 바꾸지 않는다. 세 상수 모두 끝에 개행이 없으므로
  파일 끝의 개행 1개는 로더가 걷어낸다(POSIX 관례와 상수 원문을 동시에 만족시키는
  유일한 규칙).
- 본문에 리터럴 중괄호는 현재 없다. 있을 경우 langchain f-string 템플릿과 같은
  규칙(`{{`, `}}`)으로 이스케이프하고, 자리표시자 추출도 `string.Formatter`로 같은
  규칙을 쓴다.
- 파일 읽기는 프로세스당 1회. `functools.lru_cache`는 `app/config.py`,
  `app/llm/chat_model.py`가 이미 쓰는 방식이다.
- 런타임 컨테이너에 `.md`가 실려야 한다. `Dockerfile` runtime 스테이지는
  `COPY app ./app` 뒤에 `pip install .`을 하고 `/app`에서 uvicorn을 띄우므로 소스
  트리 쪽 `.md`가 이미 잡히지만, 설치본에서도 빠지지 않게 `pyproject.toml`에
  package-data를 명시한다.
- 다른 워커(ARTEL-180)가 `app/qa/scene.py`, `app/agents/qa/tools.py`,
  `app/qa/channel.py`를 들고 있다. 건드리지 않는다.
- 베이스라인: `python -m pytest` 111 passed.

## Approach (Checklist)

- [ ] **Step 0: Recon** — 상수 3종 위치 확인, 죽은 코드 참조 grep(`QaExecutionAgent`,
      `ACT_*`/`EVAL_*`/`CHAT_*`, `QaActRequest` 등)으로 실제 미참조 확인, 베이스라인
      테스트 확보. (완료)

- [ ] **Step 1: 로더** — `app/prompts/`를 패키지로 만들고
      `app/prompts/loader.py`에 다음을 둔다.
  - `parse_prompt_file(text)`: frontmatter 파싱 + 본문 분리(끝 개행 1개 제거).
  - `placeholders_in(body)`: `string.Formatter().parse()`로 `{name}` 추출,
    `{{`/`}}`는 리터럴로 취급.
  - `available_versions(agent)`: `v<정수>` 디렉터리만 인정, 정수 기준 정렬
    (`v2 < v10`).
  - `resolve_version(agent, explicit)`: 명시 인자 > settings 기본값 > 최신.
  - `load_prompt(agent, role, version)`: `lru_cache`로 프로세스당 1회 읽기.
    frontmatter `version`이 디렉터리 이름과 다르면 실패, 선언 placeholder와 본문
    자리표시자가 정확히 일치하지 않으면 실패.
  - `validate_prompts()`: 모든 파일을 훑고, settings가 가리키는 버전이 실제로
    있는지 확인.

- [ ] **Step 2: v1 파일** — 상수 6개를 `.md`로 옮긴다. 문구 무변경.

- [ ] **Step 3: 설정** — `app/config.py`에 `qa_prompt_version`,
      `scenario_prompt_version`, `game_context_prompt_version`
      (`str | None = None`) 추가. `.env.example`에 주석으로 노출.

- [ ] **Step 4: 호출부**
  - `QaRunner(prompt_version=...)` — 런 시작 로그에 `prompt_version=` 추가.
  - `QaSessionRecord.prompt_version`, `QaExecutionService.open(prompt_version=...)`,
    `OpenQaSessionRequest.prompt_version` — `model`/`language`와 같은 경로.
    `runner_factory` 시그니처가 인자 하나 늘어난다.
  - `build_scenario_prompt(version=None)`, `build_game_context_prompt(version=None)`.

- [ ] **Step 5: 기동 검증** — `app/main.py` `create_app`에서 `validate_prompts()`
      호출. 로깅 설정 직후, 라우터 등록 전.

- [ ] **Step 6: 죽은 코드 제거** — `app/agents/qa/agent.py`,
      `app/agents/qa/schemas.py`(전부 미참조), `prompt.py`의
      `ACT_*`/`EVAL_*`/`CHAT_*`과 빌더들, `__init__.py` export.
      `LANGUAGE_DIRECTIVES`는 `runner.py`가 쓰므로 남긴다.

- [ ] **Step 7: 패키징** — `pyproject.toml`에 `[build-system]`과
      `[tool.setuptools.package-data]`로 `app/prompts/**/*.md` 포함. 휠을 실제로
      만들어 파일이 들어갔는지 확인하고, 가능하면 `docker build --target test`.

## Validation

- **Commands to run:**
  - `python -m pytest`
  - `python -m pip wheel . --no-deps -w <tmp>` 후 휠 안의 `.md` 목록 확인
  - `docker build --target test .` (도커가 있을 때만)
- **Expected output:**
  - 기존 111 + 신규 테스트 전부 통과.
  - 신규 테스트: 버전 해석 3순위, `v2 < v10` 정렬, placeholder 불일치 실패,
    없는 버전 실패, frontmatter/디렉터리 버전 불일치 실패, 리터럴 중괄호 이스케이프,
    v1 본문이 이전 상수와 바이트 단위로 같은지(3종 전부).
  - 휠 안에 `app/prompts/*/v1/*.md` 6개.

## Risks & Rollback

- **Risks:**
  - 상수를 옮기며 공백/개행이 한 글자라도 달라지면 모델 동작이 조용히 바뀐다.
    → 이전 상수 원문을 테스트에 박아 두고 바이트 비교로 막는다.
  - 빌드 백엔드가 `.md`를 떨어뜨리면 런타임에서만 터진다.
    → 휠을 실제로 만들어 확인하고, 기동 검증이 즉시 실패하게 해 둔다.
  - `runner_factory` 시그니처 변경이 `tests/test_qa_service_deliver.py`의 람다를
    깨뜨린다. → 같은 커밋에서 람다를 함께 고친다.
- **Rollback steps:** 기능 플래그 없음. 커밋 단위 `git revert`.

## Open Questions

- 없음. 레이아웃·형식·해석 순서·검증 시점은 이슈와 사전 합의로 확정돼 있다.
