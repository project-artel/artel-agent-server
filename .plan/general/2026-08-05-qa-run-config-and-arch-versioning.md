# 2026-08-05 — QA 런 설정 확정·기록과 에이전트 구조 버저닝

- Date: 2026-08-05
- GitHub Issue: None
- Jira: ARTEL-238 (Epic ARTEL-11 [Backend] Agent 서버 개발) — ARTEL-239를 blocks
- Branch: `feat/qa-런-실행-설정-확정과-에이전트-구조-버저닝-ARTEL-238`
- Status: Done
- Stacked on: PR #45 (`fix/QA-에이전트가-스텝-실패로-런을-중단하지-않게-한다-ARTEL-242`)

## Goal

QA 에이전트를 **모델 / 에이전트 구조 / 시스템 프롬프트 버전** 축으로 정량 비교할 수 있게,
한 번의 런이 무엇으로 실행됐는지를 **요청값이 아니라 해석된 값**으로 확정하고 밖으로 내보낸다.

세 가지를 만든다.

1. **`QaArchSpec`** — 지금 모듈 상수로 박혀 있는 루프 상한·툴 허용치·vision·미들웨어 조합을
   요청 가능한 데이터로 뺀다. 구조 실험이 코드 포크 없이 동시 A/B 된다.
2. **식별자** — `agent_arch`(수동 라벨) + `agent_fingerprint`(자동 해시), 프롬프트 body 해시,
   `git_sha` / `image_tag`.
3. **방출** — 해석된 설정 전체를 `POST /qa-sessions` 응답으로 돌려준다. Orchestration이
   `qa_try`에 저장한다(별도 계획: orchestration-server `.plan/general/2026-08-05-persist-qa-run-config.md`).

## Non-goals

- 옛 에이전트 구조를 버전 폴더로 보존하는 것. 코드는 git sha + 이미지 태그로 식별하고,
  재현이 필요하면 그 이미지를 다시 띄운다. 죽은 사본을 트리에 쌓지 않는다 — 아무도 안 고쳐준
  옛 구조는 조용히 썩고, 썩은 채로 돌아가면 아키텍처 탓인지 방치 탓인지 구분 못 하는
  **틀린 비교**를 낸다.
- 결과 metric 집계·비용 수집. ARTEL-233/234(LLM 호출별 토큰·비용 배치 수집)가 진행 중이고,
  이 계획은 그 수치를 **묶을 축**을 제공하는 쪽이다.
- 정답 라벨(사람이 매긴 기대 결과) 도입. 이게 없으면 정확도가 아니라 모델 간 일치도만 나온다 —
  별건으로 다룬다.
- `refactor/qa-run-scenario-step-case`(Run→Scenario→Step→Case 재구성)와의 병합. 그쪽이
  `ScenarioDraft` 모양을 바꾸지만 이 계획이 만지는 축과 교차하지 않는다. 나중에 합칠 때
  `_first_message` / `_tool_call_limit(len(steps))` 두 지점만 조정하면 된다.

## Context / Constraints

기준 커밋 `origin/develop` = `ef4f539`.

이미 있는 것:

| 위치 | 상태 |
|---|---|
| `app/api/qa_sessions.py:30` | `model` / `language` / `prompt_version` / `reasoning` 수신 |
| `app/prompts/loader.py` | 버전 디렉터리 + frontmatter 검증. 해시는 없음 |
| `app/agents/qa/runner.py:170` | `run starting` 로그가 이미 설정 전체를 찍음 — 로그로만, 밖으로는 안 나감 |
| `app/agents/qa/runner.py:229` | LangSmith `metadata`에 `qa_try_id` / `model` / `language`만 |

없는 것 = 이 계획의 대상:

| 대상 | 지금 |
|---|---|
| `BASE_TOOL_CALLS` / `TOOL_CALLS_PER_STEP` | `runner.py:50` 모듈 상수 |
| `MAX_SEARCHES/RECORDS/FORGETS_PER_RUN` | `knowledge.py:38,43,53` 모듈 상수 |
| `MAX_CAPTURES_PER_RUN` | `vision.py:29` 모듈 상수 |
| vision 여부 | `runner.py:159`에서 모델 스펙이 강제. "Opus + vision off" 실험 불가 |
| 미들웨어 조합 | `runner.py:207` 하드코딩 (`_fold_scene_views` 항상) |
| 구조 식별자 | 없음 |
| 프롬프트 내용 해시 | 없음 — `v3` 파일을 편집하면 옛 v3 런과 새 v3 런이 한 버킷에 섞임 |
| 빌드 식별자 | 없음 |

제약:

- `prompt_version=None`은 "최신"이라는 **별칭**이다. v4가 추가되면 과거 런이 어느 버전이었는지
  소실된다. 해석 시점에 확정해서 내보내야 한다.
- `reasoning=None`도 두 뜻 — "미지정" vs "모델 미지원"(`validate_reasoning`). 응답에서 구분한다.
- `build_chat_model`은 `@lru_cache`이고 `temperature=0.2`가 하드코딩이다. temperature는 이번에
  knob으로 빼지 않되(캐시 키·검증 범위가 함께 커진다) 스냅샷에는 싣는다.
- `BASE_TOOL_CALLS`는 지금 knowledge 허용치의 합으로 파생된다. 이 의미(허용치는 스텝 예산을
  잠식하지 않는다)를 spec에서도 유지한다.

### 설계 결정: 왜 HTTP 응답인가

해석된 설정을 WebSocket `STATUS` 프레임으로 흘리는 안을 버렸다. `QaAgentInboundRouter:134`의
`routeStatus`는 STATUS가 이미 2-scope(스텝 판정 vs 런 종료)이고 `result` 필드로 갈린다. 세 번째
의미를 얹으면 그 분기가 깨지기 쉽고, `qa_log.type` CHECK 제약 마이그레이션까지 따라온다.

`POST /qa-sessions`는 동기 요청이고 Orchestration은 응답 직후 `attachAndMarkRunning`에서 이미
DB를 쓴다. 같은 트랜잭션 흐름에 얹는 게 새 프레임 타입보다 훨씬 작다.

### 계약 (Orchestration과 공유)

요청 — 기존 필드 + `arch`(전부 optional, 생략 시 기본값):

```jsonc
{
  "context": { "qa_try_id": 1, "game_instance_id": 2, "test_scenario_id": 3, "scenario": {} },
  "model": "anthropic/claude-sonnet-5",
  "language": "ko",
  "prompt_version": "v3",
  "reasoning": { "effort": "high" },
  "arch": {
    "label": "v2-tool-loop",
    "base_tool_calls": 10,
    "tool_calls_per_step": 15,
    "deadline_seconds": 600.0,
    "max_searches_per_run": 6,
    "max_records_per_run": 5,
    "max_forgets_per_run": 2,
    "max_captures_per_run": 12,
    "vision": "auto",          // auto | on | off
    "fold_stale_scenes": true
  }
}
```

응답 — `session_id` + 해석된 `run_config`:

```jsonc
{
  "session_id": "…",
  "run_config": {
    "model": "anthropic/claude-sonnet-5",
    "provider": "anthropic",
    "temperature": 0.2,
    "reasoning": { "effort": "high" },
    "reasoning_supported": true,      // false면 reasoning은 null이고, 그 이유가 이 필드
    "language": "ko",
    "prompt_version": "v3",           // 별칭 아닌 확정값
    "prompt_hashes": { "system": "ab12cd34ef56", "vision_directive": "7890abcdef12" },
    "agent_arch": "v2-tool-loop",
    "agent_fingerprint": "a3f1c9d2e8b0",
    "arch": { /* 위 knob 전부, vision은 true/false로 해석됨 */ },
    "tools": ["observe_scene", "click_button", "enter_text", "…"],
    "git_sha": "ef4f539",
    "image_tag": "artel-agent-server:2026.08.05-ef4f539"
  }
}
```

`vision: "on"`인데 모델이 미지원이면 **422로 거부**한다. 조용히 끄면 실험 결과에 거짓말이 섞인다.
`"auto"`는 모델 능력을 따르고, 해석 결과가 `arch.vision`에 boolean으로 남는다.

### 설계 결정: fingerprint에 뭘 넣나

`agent_fingerprint`는 **구조만** 해시한다. 모델·프롬프트·언어는 각자 독립 축이므로 제외한다 —
넣으면 모든 축이 한 해시로 뭉개져서 "구조만 다른 두 런"을 못 묶는다.

입력: 툴 이름 + 인자 스키마, 미들웨어 이름(순서 포함), 해석된 arch knob 전부, 출력 전략.
vision이 꺼지면 툴 집합과 미들웨어가 실제로 달라지므로 지문도 갈린다 — 의도한 동작이다.

`agent_arch` 라벨은 사람이 손으로 bump 하고, 지문은 bump를 잊어도 버킷을 가른다. 라벨은 리포트
GROUP BY용, 지문은 "라벨 같은데 결과 다름" 탐지용. 둘 다 필요하다.

## Approach (Checklist)

- [x] **Step 0: Recon** — 완료. 대상 파일: `app/agents/qa/{runner,tools,knowledge,vision}.py`,
      `app/prompts/loader.py`, `app/llm/{models,chat_model}.py`, `app/config.py`,
      `app/qa/{schemas,service}.py`, `app/api/qa_sessions.py`, `Dockerfile`, `Jenkinsfile`.

- [x] **Step 1a: `app/agents/qa/arch.py` (신규)**
  - `QaArchSpec` (frozen pydantic, `extra="forbid"`) — 위 knob 전부. `vision: Literal["auto","on","off"]`.
  - `QA_ARCH_LABEL = "v2-tool-loop"` — 구조 바뀌면 손으로 bump.
  - `ResolvedArch` — `vision: bool`로 확정된 형태.
  - `resolve_arch(spec, model) -> ResolvedArch` — `vision="on"` + 미지원 모델이면 raise.
  - `arch_fingerprint(resolved, tools, middleware_names) -> str` — sha256 앞 12자.

- [x] **Step 1b: 상수를 spec 기본값으로 이관**
  - `knowledge.py` / `vision.py`의 `MAX_*_PER_RUN`은 `QaArchSpec` 필드 기본값으로 옮기고,
    모듈 상수는 그 기본값을 가리키는 이름으로만 남긴다(외부 참조 깨지 않게).
  - `build_tools(channel, state, arch: ResolvedArch)` — `supports_vision: bool` 인자를 대체.
    허용치는 spec에서 읽는다.

- [x] **Step 1c: `runner.py`**
  - `QaRunner.__init__`이 `QaArchSpec`을 받는다. `_tool_call_limit`은
    `arch.base_tool_calls + arch.max_searches + arch.max_records + arch.max_forgets
    + arch.tool_calls_per_step * max(steps, 1)` — 현재 파생 의미 유지.
  - 미들웨어 조합을 `arch`에서 만든다 (`fold_stale_scenes` 토글, vision 미들웨어는 해석된 vision).
  - LangSmith `metadata`에 `agent_arch` / `agent_fingerprint` / `prompt_version` / `git_sha` 추가.
  - `run starting` 로그는 `run_config` 딕트 하나를 찍는 형태로 정리 (지금 인자 나열).

- [x] **Step 1d: `app/prompts/loader.py`**
  - `PromptFile`에 `body_sha256: str`. `V14__add_content_hash_to_project_document`와 같은 패턴.
  - `validate_prompts`는 그대로.

- [x] **Step 1e: `app/config.py`** — `git_sha: str | None`, `image_tag: str | None`.

- [x] **Step 1f: 해석 지점 하나로 모으기**
  - `app/qa/service.py`의 `open()`이 세션 열 때 `RunConfig`를 **한 번** 만든다.
    (모델 스펙 + 설정 + spec 기본값은 전부 open 시점에 알 수 있다.)
  - 프롬프트 로딩도 여기로 당긴다 — 지금은 `run()`에서 읽어서 open 응답이 프롬프트 버전을 모른다.
    로딩 실패가 세션 수립 전에 드러나는 이점도 있다.
  - `QaRunner`는 확정된 `RunConfig`를 받아 쓰기만 한다.

- [x] **Step 1g: API** — `OpenQaSessionRequest.arch`, `OpenQaSessionResponse.run_config`.

- [x] **Step 1h: 빌드 식별자 주입** — `Dockerfile`에 `ARG GIT_SHA` / `ARG IMAGE_TAG` → `ENV`.
      `Jenkinsfile`이 `--build-arg`로 채운다.

- [x] **Step 2: Tests**
  - `tests/test_qa_arch.py` (신규): 기본 spec의 지문이 안정적일 것 / knob 하나 바꾸면 지문이
    바뀔 것 / vision 해석에 따라 지문이 갈릴 것 / `vision="on"` + 텍스트 전용 모델은 422.
  - `tests/test_prompts_loader.py`: body 해시가 내용에 따라 바뀌고 frontmatter 변경에는 안 바뀔 것.
  - `tests/test_api.py`: `run_config` 응답 계약 — `prompt_version`이 별칭 아닌 확정값,
    미지원 모델에서 `reasoning=null` + `reasoning_supported=false`.
  - `tests/test_qa_model_reasoning.py` / `test_qa_knowledge.py`: `build_tools` 시그니처 변경 반영.
  - `insomnia-sync` 스킬로 `agent-server.yaml` 갱신 (요청/응답 모양이 바뀐다).

- [x] **Step 3: Rollout**
  - `arch` 미전송 = 현재 동작. Orchestration이 안 보내면 아무것도 안 바뀐다 — 배포 순서 무관.
  - `run_config`는 응답에 추가되는 필드라 기존 소비자(`session_id`만 읽음)를 안 깨뜨린다.
  - 롤백은 `git revert` 하나. 스키마 없음.

## Validation

- **Commands to run:**
  - `python -m pytest`
  - `python -m uvicorn app.main:app --reload` 후 `POST /qa-sessions`에 `arch` 넣고/빼고 각각 호출,
    응답 `run_config` 확인.
  - 같은 요청 두 번 → `agent_fingerprint` 동일. `arch.tool_calls_per_step`만 바꿔 재호출 → 지문 변경.
- **Expected output:** 전 테스트 통과. `arch` 생략 시 `run_config.arch`가 현재 상수값과 일치.

## Risks & Rollback

- **Risks:**
  - `build_tools` 시그니처 변경이 테스트 여러 개를 건드린다. 기계적이지만 diff가 넓다.
  - 프롬프트 로딩을 `run()`에서 `open()`으로 옮기면 실패 시점이 바뀐다 — 세션 수립 자체가
    실패하게 되고, Orchestration은 이를 `SERVICE_UNAVAILABLE`로 본다. 의도한 개선이지만
    에러 경로가 달라지는 건 명시해 둔다.
  - `QaArchSpec`을 요청으로 열면 사용자가 상한을 크게 넣어 예산을 태울 수 있다. 각 필드에
    pydantic `le=` 상한을 건다.
- **Rollback steps:** `git revert`. 스키마·마이그레이션 없음.

## PR #45 위로 쌓으면서 생긴 변경

베이스에 압축(compaction) 미들웨어가 들어와 있었다. 이건 구조의 일부다 — 켜지면 미들웨어가
하나 늘고 `compact_context` 툴이 하나 붙으며, 런 도중 모델이 읽는 내용이 다시 쓰인다. 지문이
못 보면 "압축 켠 런"과 "끈 런"이 같은 버킷에 들어간다. 그래서:

- `QaArchSpec`에 `compaction` + 트리거/보존/최소증가/트림 knob 추가. **`None` = 배포 설정을
  따른다**(`QA_COMPACTION_*`). 숫자를 spec 기본값으로 복사하면 진실이 둘이 되고, 먼저 낡는
  쪽은 사본이다. `resolve_arch`가 `None`을 설정값으로 메운다.
- `middleware_names_for` / `build_middleware`가 압축을 포함한다. 런이 실제로 쓰는 목록과
  지문이 계산되는 목록이 **같은 목록**이어야 구조가 정체성 없이 바뀌는 일이 없다.
- `structure_of`가 압축 미들웨어의 `compact_context`까지 툴 목록에 센다. 미들웨어가 자기
  툴을 들고 오므로 `build_tools`만 봐서는 안 보인다.
- 요약은 자기 모델·자기 프롬프트 버전으로 돈다. `RunConfig.compaction_model` /
  `compaction_prompt_version` + `prompt_hashes["summary"]`로 기록한다 — 런의 모델·프롬프트
  축이 설명하지 못하는 축이라서.
- `QaRunner(settings=...)`는 사라졌다. 압축 설정도 `RunConfig`를 통해 들어온다.
- `_append_current_scene`이 `run()` 안의 클로저였는데 모듈 레벨 팩토리로 올렸다.
  `build_middleware`가 조립하려면 밖에서 보여야 한다.
- `DEFAULT_RESOLVED_ARCH` 상수 → `default_resolved_arch()` 함수. 압축 기본값이 설정에서
  오므로 임포트 시점에 환경을 읽으면 안 된다.

## Open Questions

- `QA_ARCH_LABEL` 초기값을 `v2-tool-loop`로 둘지 `v1`로 리셋할지. 지금 구조는 단일 구조화 호출
  방식을 대체한 두 번째 구조라 `v2`가 맞아 보이지만, 기록이 없는 첫 라벨이니 정하기 나름.
