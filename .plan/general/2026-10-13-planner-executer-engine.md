# artel-agent-server 에 planner–executer 엔진 붙이기 — 구조와 단계 계획

상태: **계획**. 어느 서비스 레포도 아직 바꾸지 않는다. 2026-10-13 작성, 같은 날 결정 D1~D6 반영.
Jira: Story ARTEL-998, 구현 Subtask ARTEL-1002~1013(ARTEL-1006 제외, 8절의 스택 순서).

PoC(`artel-kimgooner-jev-poc`, `jev-poc-skills-16` / prompt `skills-v13`)의 구조를, 지금 서비스 중인
`artel-agent-server` 의 QA agent(단일 agent 도구 루프)를 **그대로 둔 채** 실행마다 고를 수 있는 두 번째 엔진으로
붙인다. PoC 레포의 10/7 초안 `docs/integration-plan.md` 를 대체한다.

PoC: https://github.com/project-artel/artel-kimgooner-jev-poc (`main` `a2d1e72`). 아래 "PoC `path`" 는 이 레포 기준이다.

조사 기준 (각 레포 `origin/develop`, 2026-10-13). 아래 `path:line` 은 이 커밋 기준이다.

| 레포 | 커밋 | 비고 |
|---|---|---|
| `artel-agent-server` | `3a7a666` (PR #217, ARTEL-988) | `QA_ARCH_LABEL = "v24-macro-wait-until"`, prompt `qa_run/v21` |
| `artel-orchestration-server` | `48a39f98` (PR #296, ARTEL-986) | 이 계획에서 바꾸지 않는다 |
| `artel-sdk` | `98984be` (PR #139, ARTEL-957) | 실시간 트랙(6절)에서만 |

---

## 0. 원칙과 한눈에

1. **기존 서비스를 고치지 않는다.** 새 엔진의 코드·프롬프트·저장소는 모두 새로 만드는 우리 쪽에 둔다. 기존 코드에
   손대는 곳은 엔진을 고르는 두 지점(1-5)뿐이다: `QaArchSpec` 의 엔진 축과 `runner_factory` 분기.
   orchestration·SDK·기존 QA agent·매크로 코드·기존 JEV 호출부는 바꾸지 않는다.
2. **기존 기능은 가져다 쓴다.** 채널, `pulse`, scene-context, STATUS 판정 형식, 사용량 버퍼, 지식 검색, 매크로
   읽기·검색·실행 — 이미 있는 공개 인터페이스를 호출만 한다.
3. **모델 호출은 `app/llm/` 을 지나 사용량이 `QA_RUN` 으로 잡힌다.**
4. **측정이 단계를 넘긴다.** agent-server `AGENTS.md` "QA agent structure"(전후 실측, `run_config` 네 축, 판 수).

```
orchestration ── POST /internal/qa-sessions {arch, context, ...} ─┐   (arch 는 내용을 보지 않고 넘긴다)
                                                                   ▼
QaExecutionService.runner_factory(config)                 app/qa/service.py:72   ← 분기 (기존 코드, 최소 수정)
        │ config.arch.engine
  ┌─────┴──────────────────────────┐
  ▼ "agent" (기본, 그대로)          ▼ "planner_executer" (새로)        app/agents/planner_executer/
QaRunner                          PlannerExecuterRunner
create_agent 도구 루프              ├─ prompt maker   planner 프롬프트를 모듈로 조립 (D4)
                                   ├─ planner        sol 모델 — 스텝마다 step module 을 쓰고 편집 블록으로 고친다
                                   ├─ executer       매 턴 func 를 실행, JEV 로 고른다, 매크로를 부른다
                                   └─ playbook       에이전트가 스스로 쓰려고 쌓는 step module (Redis, 우리 이름공간)
        └──────────── 공유 (호출만) ─────────────┘
QaRunChannel · SceneMemory/PulseMemory · scene_context(매크로 목록 포함) · STATUS/LOG/TOOL ·
UsageBuffer · QaRunState(build_summary) · KNOWLEDGE_SEARCH · MACRO_READ/MACRO_SEARCH · run_macro
```

| 단계 | 목표 | 바꾸는 곳 | 끝났다는 기준 |
|---|---|---|---|
| 0 | 기준선과 이슈 | (측정만) | 현행 엔진 L2 기준 수치 |
| 1 | 엔진을 agent-server 에 | agent-server 새 패키지 + 분기 두 곳 | `engine="planner_executer"` 로 L2 가 돌고 콜드 통과 수가 현행 이상 |
| 2 | 플레이북·게임 문장을 실행 사이에 | agent-server 새 패키지(Redis) | 같은 빌드 둘째 판의 planner 호출·비용이 줄고 통과 수 유지 |
| 3 | 생성 코드 격리 | agent-server 새 패키지 | stage 에서 켜기 전 필수 |
| 4 | 화면·통계 | 별도 승인 (artel-home 등) | — |
| 5 | 실시간 게임 | artel-sdk (별도 결정) | — |

---

## 1. 결정 (2026-10-13)

### D1. 이름

| 대상 | 이름 |
|---|---|
| 엔진 (`arch.engine`) | `planner_executer` |
| sol 모델로 step module 을 쓰고 고치는 부분 | **planner** |
| 매 턴 func 를 실행하고 JEV 를 부르는 부분 | **executer** |
| PoC 의 skill (`when`/`run` 한 쌍) | **func** — 모듈의 표는 `FUNCS`, 함수는 `when_<name>`/`run_<name>` 그대로 |
| 스텝의 모듈 | **step module** |
| 에이전트가 스스로 쌓는 저장소 | **playbook** |
| 장르별 조립 단위 | **genre module** |
| planner 프롬프트 조립기 | **prompt maker** |
| func 가 받는 게임 핸들 | `g` |

`skill` 은 쓰지 않는다 — agent-server 에서 이미 prompt 문서(`load_skill`, `arch.skills`, `skill_*.md`)를 뜻한다.
`function` 은 예약어는 아니지만 너무 일반적이라 `func` 로 한다.

### D2. 실행 형식 — 자유 Python, 격리는 필수

PoC 와 같은 자유 Python step module 로 시작한다(PoC 의 모든 측정이 이 형식이다). `exec` 이므로 **단계 3 의 프로세스
격리 없이는 stage 에서 켜지 않는다.** 매크로 DSL(exec 없음, `app/agents/qa/macro/`)로 옮길지는 2단계 측정 뒤 다시
판단한다.

### D3. 플레이북과 매크로 — 둘 다 쓴다, 기존 것은 고치지 않는다

| | playbook | 매크로 |
|---|---|---|
| 누가 만드나 | **에이전트가 스스로**, 자기가 다시 쓰려고 | **사용자가** "이 기능을 써라"고 넣어 준다 |
| 형식 | Python step module (`done`/`check`/`progress`/`FUNCS`) | DSL (`app/agents/qa/macro/`) |
| 저장 | agent-server Redis, 우리 이름공간 (2-1) | orchestration `macro` 테이블 (기존) |
| planner–executer 의 쓰임 | 스텝 시작 때 꺼내 쓴다 | planner 가 목록을 보고, func 안에서 `g.run_macro` 로 부른다 |

기능이 겹쳐도 된다. 매크로는 읽고 실행만 하고 **등록·수정하지 않는다**(`MACRO_REGISTER` 를 보내지 않음).

매크로를 쓰는 길 (전부 기존 공개 인터페이스, 수정 없음):
- 목록: `channel.scene.scene_context.macros`(최대 30, `SceneMacro`, `app/qa/scene_context.py:254`).
- 검색: `channel` 의 `MACRO_SEARCH` 메서드(`app/qa/channel.py:647`) — 이름·파라미터·docstring·화면.
- 정의: `MACRO_READ`(`channel.py:628`)로 받아 기존 파서 `macro_definition_from_source`(`app/agents/qa/macro/parser.py`)로
  읽는다.
- 실행: 기존 `run_macro(host, definition, arguments, step)`(`app/agents/qa/macro/runner.py:343`). `MacroHost`
  (`runner.py:145`: `run`, `memories`, `wait_for_pulse`, `checkpoint`)의 구현을 **우리 패키지에** 둔다
  (`MacroHostAdapter`). `checkpoint` 는 판정 없이 진행(planner–executer 에는 사람 확인 단계가 없다).
- `g` 에 `await g.run_macro(name, **arguments) -> MacroResult`, `g.macros() -> [이름·파라미터·설명]` 을 더한다.
  결과의 `applied`/`failure` 를 func 가 읽는다.

### D4. prompt maker — planner 프롬프트를 모듈로 조립

planner 가 받는 모든 텍스트를 한 곳에서 조립한다(`app/agents/planner_executer/prompt_maker.py`). 각 조각은 독립
모듈이고, 무엇이 들어갔는지(조각 이름·버전·해시·글자 수)를 `run_config` 에 남긴다.

| 조각 | 출처 | 게임마다 다른가 |
|---|---|---|
| core 규칙 (`rules.md`, `add_rules.md`, `review_rules.md`) | `app/prompts/qa_planner/v1/` | 아니다 — 모든 게임에 참인 것만 |
| `g` API (`api.md`) | 같은 곳 | 아니다 |
| genre module (`genres/*.md`) | 같은 곳. 어떤 것을 붙일지는 `arch.planner.genres` | 장르마다 |
| 게임 지도 | `scene_context`(기존) | 빌드마다 |
| 기획서·만든 사람의 사실 | 기존 지식 경로: 프로젝트 문서 PDF → `/extract` → `knowledge`(source=DOCS). prompt maker 가 스텝 작성 전 `KNOWLEDGE_SEARCH` 로 가져온다 | 게임마다 |
| 게임이 화면으로 보여 준 문장(`told`) | 판 중 수집 + playbook 저장소(2-1) | 빌드마다 |
| 쓸 수 있는 매크로 | `scene_context.macros`, 필요 시 `MACRO_SEARCH` | 빌드마다 |
| 같은 씬에서 통과한 step module | playbook | 빌드마다 |
| 스텝·현재 상태·편집 대상 module | 실행 중 | 매 호출 |

조각별 자격(무엇이 core 에, 무엇이 장르에, 무엇이 게임 지식에 들어가는가)은 PoC `docs/prompt-rules.md` 의 기준을
그대로 agent-server 문서로 옮긴다. 핵심: core 에는 계약·도구의 성질·QA 판정 의미만, 장르 모듈에는 구조적 패턴만,
게임 지식에는 사실만 — 전략과 정답은 어디에도 넣지 않는다.

`game_build.notes`(V4, 2000자)는 지금 agent 로 오지 않고, 오게 하려면 orchestration 을 고쳐야 하므로 쓰지 않는다.
만든 사람의 사실은 기획서 업로드(기존 지식 경로)로 받는다.

### D5. 엔진 축과 `fingerprint`

- `QaArchSpec`(`app/agents/qa/arch.py:571`, `extra="forbid"`)에 `engine: Literal["agent", "planner_executer"] =
  "agent"` 와 `planner: PlannerExecuterArchSpec | None = None` 을 더한다. `engine="agent"` 에 `planner` 가 오면 거부.
- 필드가 늘면 기본 구조의 `agent_fingerprint`(`arch_fingerprint`, `:844`)도 바뀐다 → `QA_ARCH_LABEL` 을 올리고(v25) 위에 단락을
  쓴다("엔진 축을 더했다. 도구 루프의 동작은 같고 `fingerprint` 만 바뀌었다"). `tests/test_qa_arch.py` 의
  `_LABEL_THIS_STRUCTURE_WAS_PINNED_UNDER`(:518), `_EXPECTED_DEFAULT_FINGERPRINT`(:643)를 같은 커밋에서 갱신.
- planner–executer 의 `arch_fingerprint` 는 `"kind": "planner-executer"`, `PlannerExecuterArchSpec` + `g` 함수 목록을 해시한다.
  라벨은 따로 센다(예: `pe1-...`, 50자 이하). 통계는 라벨로 묶이므로 구조가 바뀔 때마다 라벨을 올린다.

### D6. 기준선

현행 엔진(`v24-macro-wait-until`, `qa_run/v21`)으로 WordVenture L2 를 여러 판(5 권장), `qa-run-local` 스킬 절차.
같은 빌드(2026-09-19, WV `20623b1`)·같은 planner 모델로 비교한다. PoC 수치는 PoC 서버 기준이라 바로 비교하지 않는다.

---

## 2. 단계 1 — 엔진 (`app/agents/planner_executer/`)

orchestration·SDK 는 바꾸지 않는다. 실행 생성은 `artel qa run --arch '{"engine":"planner_executer","planner":{...}}'`.

### 1-1. 패키지 구성

| 새 파일 | PoC 원본 | 내용 |
|---|---|---|
| `arch.py` | `jev_poc/arch.py` | `PlannerExecuterArchSpec`(frozen, extra=forbid): `max_turns_per_step`, `step_budget_seconds`, `stall_seconds`, `progress_stall_seconds`, `max_additions`, `max_name_rewrites`, `max_reviews`, `spin_turns`, `step_retries`, `max_fixes`, `max_planner_calls`, `executer_model`(기본 `typesafe/jev-1.13`), `genres`, `playbook`(1단계 off) |
| `prompt_maker.py` | `skill_planner.py` 의 조립부 | D4 |
| `planner.py` | `jev_poc/skill_planner.py` | 호출은 `build_chat_model`(`app/llm/chat_model.py`) → 사용량이 `UsageCallback` 으로 잡힌다. 작성(전체 module)과 편집(SEARCH/REPLACE) |
| `executer.py` | `jev_poc/skill_runner.py` | 턴 루프, func 선택(우선순위 → 같은 func 변형 묶기 → JEV, 0.85 미만이면 작성 순서), 대기·재시도·setback 수정·호출 예산·유예·검토·교체 검사 |
| `decisions.py` | `jev_poc/llm.py` `SystemOneSelector` | JEV decisions 클라이언트(우리 것). 후보 밖 답 거부, 확률 검증, 2.5초 제한, 사용량을 `UsageBuffer` 에 기록 |
| `g.py` | `jev_poc/game.py` | `channel.scene.pulse` 를 읽고 `channel.dispatch_actions` 로 입력. live 감지·대기 끊기·`settle`·`input_lag`·`told`·`missing_reads`·사람 포인터 재시도·press-elsewhere. `run_macro`/`macros` (D3) |
| `macro_host.py` | (새로) | 기존 `MacroHost` 프로토콜의 우리 구현 |
| `edits.py`, `names.py`, `sandbox.py`, `game_map.py` | 같은 이름 | 그대로. `game_map` 은 `scene_context` 를 그린다 |
| `playbook.py` | `jev_poc/playbook.py` | 1단계는 실행 안 메모리 |
| `runner.py` | (새로) | `PlannerExecuterRunner.run_with_deadline(channel, scenario)` — 서비스가 기대하는 모양 |

새 `reading` 알림: PoC 는 채널에 `reading_listeners` 를 더했다. 기존 `QaRunChannel` 을 고치지 않기 위해, executer 가
`channel.wait_for_reading`(`channel.py:444`)으로 새 `reading` 을 기다리는 자체 루프에서 `told`·지나간 씬·live 감지를
갱신한다. 한꺼번에 온 `reading` 처리(`settle`)도 이 루프에서 한다.

### 1-2. 프롬프트 (`app/prompts/qa_planner/v1/`)

PoC `skills-v13` 을 v1 로 들여온다(용어만 D1 대로: `SKILLS` → `FUNCS`, skill → func, selector → executer).

| 파일 | 역할 |
|---|---|
| `rules.md` | core 규칙 |
| `add_rules.md`, `review_rules.md` | 판 도중 고칠 때(편집 블록) |
| `api.md` | `g` (+ `run_macro`, `macros`) |
| `genres/ui-flow.md`, `genres/turn-based.md`, `genres/real-time.md` | genre module |
| `hints.md` | 조건부 힌트 초안 (연결은 나중) |

기존 프롬프트 로더에 새 agent 디렉터리를 등록하는 것은 로더 설정 추가다(기존 `qa_run` 은 그대로).
`python -m app.prompts.lock --write` 로 잠금 파일 갱신.

### 1-3. 판정과 프레임 — 기존 모양 그대로

- 스텝 판정 STATUS 는 현행 `report_step` 과 같은 모양(`status=COMPLETED|FAILED`, `step`, `case_id`,
  `is_verification`, `message`, `result` 없음). orchestration `ExpectedStepsGrader` 가 이것만 읽는다.
- `QaRunState`(`app/agents/qa/tools/state.py`)를 그대로 써서 `build_summary`·케이스 요약을 공유.
- 새 프레임 타입은 만들지 않는다. 입력은 `TOOL`/`TOOL_RESULT`(`payload.tool` = `g` 함수 이름, 비어 있지 않은
  `message`), 턴은 `LOG`(`payload.kind="turn"`, 48KB 이하 — agent→orchestration 64KiB 기본 상한), step module 과
  편집은 `LOG`(THOUGHT, 6000자에서 자름). planner 프롬프트 전문은 보내지 않는다.
- 게임 시간 멈춤은 `ACTION` 의 `pause_time`/`resume_time`(SDK 지원). `channel.dispatch_actions` 가 메서드를
  거르지 않는지 확인한다.

### 1-4. JEV 사용량

우리 `decisions.py` 가 부르는 JEV 호출만 기록한다(`set_usage_scope("QA_RUN", qa_try_id)` 범위, provider
`typesafe`). 기존 JEV 호출부(`JevScenarioRouter`, `PulseRelevanceJudge`)는 기록하지 않고 있지만 이 계획에서 고치지
않는다 — 별도 이슈로 남긴다. orchestration 사용량 기록에 역할 칸이 없으므로 planner 와 executer 는 모델 이름으로
구분된다.

### 1-5. 기존 코드에 손대는 두 곳

| 파일 | 변경 |
|---|---|
| `app/agents/qa/arch.py` | `engine`, `planner` 필드, 라벨 v25 와 단락 (D5) |
| `app/qa/service.py` | `runner_factory` 가 `config.arch.engine` 에 따라 `QaRunner` / `PlannerExecuterRunner` (`:72,222`) |

그리고 그에 딸린 `tests/test_qa_arch.py` 고정값 갱신. 이 밖의 기존 파일은 바꾸지 않는다.

### 1-6. 테스트

- 가짜 게임 하네스: PoC `tests/fakes.py`(`FakeGame`)를 우리 테스트 디렉터리로.
- 옮길 테스트: executer(턴 기록, 교체 검사, 재시도, setback 수정, live 대기 끊기, 시간 멈춤, 유예, JEV 0.85),
  `names`, `edits`, prompt maker 조립(조각·해시), sandbox, decisions.
- 매크로: 기존 파서로 만든 정의를 `MacroHostAdapter` 로 실행하는 테스트.
- `fingerprint`: `engine` 미지정 실행은 `agent_fingerprint` 외에 바뀌지 않는다(현행 테스트 통과), planner–executer `fingerprint` 고정.

**1단계를 마쳤다는 기준**
- `engine="planner_executer"` 로 WordVenture L2 가 끝까지 돌고 스텝 판정이 orchestration 채점에 잡힌다.
- 콜드 판 통과 수가 D6 기준선 이상. 비용·시간·판 수를 나란히 적는다.

---

## 3. 단계 2 — playbook 을 실행 사이에 (orchestration 변경 없음)

### 2-1. 저장소: agent-server Redis, 우리 이름공간

agent-server 는 이미 Redis 를 쓴다(`app/main.py:45`, 세션 저장 `app/qa/store.py`). 같은 연결에 **우리 키
접두어**로 저장한다. 기존 세션 저장소 코드는 쓰지도 고치지도 않는다.

```
pe:playbook:{project}:{build}:{case_id}:{case_hash}:{prompt_version}   → step module (JSON)
    {source, status: PASSED|CANDIDATE, passes, fails, start_scene, qa_try_id, updated_at}
pe:playbook-events:{project}:{build}                                   → 변경 기록 (stream, 최근 N)
pe:told:{project}:{build}                                              → 게임이 보여 준 문장 (list, 6000자 상한)
```

- **playbook 은 캐시다.** 잃어도 다음 판이 콜드로 다시 쓸 뿐(비용만 든다). 그래서 Redis 의 지속성 설정에 기대지
  않는다. 만료는 빌드 단위로 길게(예: 30일) — 운영 Redis 의 메모리 정책을 확인해 정한다.
- 키에 prompt 버전을 넣는다(PoC 는 넣지 않아 v10~v12 로 쓴 module 이 v13 판에서 재사용됐다).
- `content_map_mode` 는 agent 로 오지 않는다(orchestration 이 자체 적용). 그래서 playbook 사용 여부는
  `arch.planner.playbook` 으로 켜고 끈다. 측정 때는 콜드/웜을 명시한다.

### 2-2. playbook 규칙 (PoC 그대로)

- 꺼내기 전 이름 검사, 통과한 것은 통과한 것으로만 교체, 후보는 최신 편집으로, 연속 2번 실패하면 버림.
- 같은 시작 씬에서 통과한 module 을 새 케이스 작성 때 prompt maker 가 참고로 넣는다.

**2단계를 마쳤다는 기준**: 같은 빌드에서 콜드 → 웜 연속 실행 여러 쌍. 웜 판의 planner 호출·비용이 줄고(PoC L2 한
쌍: $0.42 → $0.09, 호출 15 → 4) 통과 수가 유지된다.

---

## 4. 단계 3 — 생성 코드 격리 (stage 에서 켜기 전 필수)

- step module 을 별도 프로세스에서 실행한다: 시간·메모리 상한, 네트워크·파일 차단, 하위 프로세스 금지.
- `g` 호출은 프로세스 경계를 넘는 RPC: 읽기는 상태 스냅숏을 넘기고, 입력·대기·매크로 실행은 부모 프로세스가 한다.
- 이 단계 전에는 로컬 측정 전용이다.

---

## 5. 단계 4 — 화면·통계 (별도 승인)

기존 서비스(artel-home, orchestration)를 고치는 일이라 이 계획의 범위 밖이다. 필요해지면 따로 제안한다:
QA 타임라인의 턴 띠·func 표·편집 이력, 사용량 역할 칸, agent WebSocket 프레임 상한 명시. 그 전까지는 기존 화면이
`LOG`·`TOOL`·STATUS 를 그대로 보여 준다.

---

## 6. 단계 5 — 실시간 게임 (별도 트랙)

executer 쪽 대응(live 감지, 대기 끊기, `settle`, 입력 지연, 시간 멈춤)은 1단계에 들어간다. 남는 두 가지는 SDK
결정이다: `pulse` 전달 1초 묶음(PoC 는 SDK 사본에서 0.1초로 바꿔 시험), 읽을 필드가 없는 오브젝트의 위치 보고(PoC 는
게임 빌드에 테스트용 센서). WordVenture 평가에는 필요 없다.

---

## 7. 위험

| 위험 | 대응 |
|---|---|
| 기본 `agent_fingerprint` 가 바뀌어 기존 통계가 끊긴 것처럼 보임 | 라벨 단락에 "동작은 같고 `fingerprint` 만 바뀜" |
| exec 보안 | 단계 3 전 로컬 전용 |
| 프레임 64KiB 초과 | 턴 로그 48KB, module 6000자 |
| Redis 메모리 | 빌드 단위 만료, 키 수 상한, 캐시로 취급 |
| 매크로 실행이 사람 확인(checkpoint)을 기대 | 어댑터가 판정 없이 진행, 결과를 func 에 돌려줌. 막히는 매크로는 실패로 |
| 게임 지식에 정답이 섞임 | prompt maker 조각 기준(D4), 조각 해시를 `run_config` 에 |
| 한 판으로 결론 | 단계마다 여러 판, 판 수를 PR 에 |

---

## 8. 작업 관리

Story ARTEL-998(`작업`, 에픽 ARTEL-11). Subtask 는 레포마다, 구현은 agent-server 한 레포다.

| Subtask | 레포 | 내용 |
|---|---|---|
| ARTEL-999 | agent-server | 이 계획 문서 |
| ARTEL-1000 | orchestration | 이 계획 문서(바꾸지 않는다는 기록) |
| ARTEL-1001 | sdk | 이 계획 문서(실시간 트랙 참고) |

### 8-1. 구현 스택 (agent-server, PR 하나에 diff 약 500줄 이하)

리뷰를 작게 하려고 큰 항목마다 PR 하나로 나누고, `gh stack` 으로 쌓는다. 아래 번호가 쌓는 순서이고, 각 Subtask 는
바로 위 번호를 `blocks` 로 막는다(먼저 머지되는 쪽이 inward). 줄 수는 테스트 포함 예상치다.

| # | Subtask | 단계 | 내용 | 약 |
|---|---|---|---|---|
| 1 | ARTEL-1002 | 1 | `arch.engine`·`planner` 필드, 라벨 v25, `runner_factory` 분기, FAILED 만 보고하는 골격 runner. 현행 엔진 기준선(D6) | 200 |
| 2 | ARTEL-1003 | 1 | `g` 읽기, `names`, `sandbox`, `game_map` | 500 |
| 3 | ARTEL-1004 | 1 | `g` 입력·대기, live 대기 끊기, `input_lag`, `settle`, `told` | 500 |
| 4 | ARTEL-1005 | 1 | executer 턴 루프, `done`/`lost`/`check`, STATUS·`LOG`·`TOOL` (고정 module 로 시험) | 500 |
| 5 | ARTEL-1007 | 1 | `decisions.py`(JEV), 0.85 기준, 변형 묶기, `g.choose`, 사용량 기록 | 350 |
| 6 | ARTEL-1008 | 1 | planner, prompt maker, `qa_planner/v1` 과 genre module. 여기서 L2 가 끝까지 돈다 | 500 |
| 7 | ARTEL-1009 | 1 | 판 도중 편집 블록, 교체 검사, 호출 조건·예산·유예·검토 | 500 |
| 8 | ARTEL-1010 | 1 | `step_retries`, walk-back, `setbacks` 수정 | 400 |
| 9 | ARTEL-1011 | 1 | `MacroHostAdapter`, `g.run_macro`/`g.macros` (D3) | 300 |
| 10 | ARTEL-1012 | 2 | playbook·`told` 를 Redis 에 (2-1) | 350 |
| 11 | ARTEL-1013 | 3 | step module 프로세스 격리 | 500 |

- 1번 PR 이 기존 코드에 손대는 유일한 PR 이다(1-5). 나머지는 새 패키지 안에서만 자란다.
- 6번 전까지는 `engine="planner_executer"` 실행이 스텝을 끝까지 풀지 못한다. 그 전 PR 의 검증은 단위 테스트와
  `FakeGame` 이다. L2 실측은 6번부터, 1번의 기준선과 나란히.
- 500줄을 넘을 것 같으면 PR 을 더 나누지 말고 그 PR 안에서 범위를 줄여 다음 번호로 넘긴다.
- 기존 JEV 호출부의 사용량 미기록은 별도 이슈로.
- PR 은 draft + 담당자 + 라벨, 구조 변경(1-5) PR 에 전후 실측. ready 전환은 사람이 한다.
- PoC 레포는 참조용으로 동결.

---

## 부록 A. PoC 에서 옮길 것

| 옮김 | 옮기지 않음 |
|---|---|
| `skill_runner.py`(→ `executer.py`), `game.py`(→ `g.py`), `skill_planner.py`(→ `planner.py`·`prompt_maker.py`), `edits.py`, `names.py`, `sandbox.py`, `game_map.py`, `playbook.py`, `arch.py`, `llm.py` 의 `SystemOneSelector`(→ `decisions.py`) | `jev_poc/wire/*` (agent-server 원본의 복사본) |
| `prompts/qa_skills/v13/*` (→ `qa_planner/v1`) | `server.py`, `usage_report.py`, `ui/*` |
| `tests/fakes.py`, 엔진 테스트 | `game_notes.py` (→ 기획서·지식 경로, D4) |
| `docs/prompt-rules.md` (→ agent-server 문서) | |

## 부록 B. PoC 측정 요약 (PoC 서버 기준, 각 1판)

| 판 | 구성 | L2 통과 | 비용 |
|---|---|---|---|
| 121 | 콜드, `skills-v10` | 7/9 | $0.419 |
| 122 | 웜(playbook 8/9 재사용) | 7/9 | $0.089 |
| 127 | 콜드, `skills-v13` + 장르 + 노트 v2 + 화면 문장 | 7/9 | $0.397 |

스텝 8(바다 스테이지)은 낭비 없는 배분을 요구하는 밸런스이고 정답을 노트에 넣었을 때(try 123, 무효)만 이겼다. 엔진
비교 때 스텝 1~7 과 스텝 8 을 나눠 본다.
