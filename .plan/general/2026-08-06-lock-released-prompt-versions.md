# 2026-08-06 — 릴리즈된 프롬프트 버전을 CI에서 잠근다

- Date: 2026-08-06
- GitHub Issue: None
- Jira: ARTEL-273 (Epic ARTEL-11 [Backend] Agent 서버 개발)
- Branch: `chore/릴리즈된-프롬프트-버전을-ci에서-잠근다-ARTEL-273`
- Status: Done

## Goal

이미 릴리즈된 프롬프트 버전 디렉토리(`app/prompts/<agent>/v<n>/*.md`)의 body가
**바뀌면 CI가 빨갛게 뜬다**. 새 버전 추가는 lock 파일을 같이 갱신해야만 통과한다.

`app/prompts/loader.py`의 docstring이 이미 규칙을 적어뒀다 — "editing `v3` in place
leaves every run before and after the edit filed under the same name, and the
comparison silently averages two different prompts". 지금 이건 **관례일 뿐 강제가 없다**.
이 계획은 그 문장을 실행 가능한 체크로 승격시킨다.

### git conflict로는 못 막는 이유

병렬 worktree 작업에서 실제로 밟히는 경로:

- 브랜치 A: `qa_run/v9/` 추가
- 브랜치 B: `qa_run/v7/system.md`에 문단 하나 추가

**서로 다른 파일이라 clean merge.** conflict 안 뜬다. 그런데 merge 후
`prompt_version=v7`로 기록된 과거 QA run들이 전부 재현 불가가 된다.
`2026-08-05-qa-run-config-and-arch-versioning.md`가 body 해시를 런마다 밖으로
내보내게 만들었지만, 그건 *사후 식별*이지 *사전 차단*이 아니다.

기존 테스트도 이걸 못 잡는다. `tests/test_qa_prompt_version.py`의 버전 간 diff
어서션은 전부

```python
for paragraph in v6.split("\n\n"):
    assert paragraph in v7
```

형태 — **삭제만 잡고 추가는 통과**한다. 완전 고정은 v1 하나뿐
(`tests/test_prompts_v1_regression.py`).

### 현재 커버리지 실측 (25개 `.md` 기준)

| 보호 수준 | 파일 |
|---|---|
| 본문 완전 고정 | `qa_run/v1/{system,vision_directive}`, `scenario/v1/{system,human}`, `game_context/v1/{system,human}` — 6개 |
| 상위집합 어서션만 (추가는 통과) | `qa_run/v2,v4/system` — 2개 |
| 인접 버전 어서션에 간접 노출 | `qa_run/v3,v5,v6/system`(다음 버전이 상위집합을 요구), `qa_run/v7,v8/system`·`v7,v8/vision_directive`(`v8 == v7`) — 7개 |
| **보호 전무** | `qa_run/v2,v3,v4,v5,v6/vision_directive`, `scenario/v2/{system,human}`, `knowledge_query/v1/{system,human}`, `qa_compaction/v1/summary` — **10개** |

**25개 중 10개는 지금 아무 테스트도 건드리지 않는다.** 구멍이 "v7 몰래 고치기"보다
넓다. lock은 25개 전부를 같은 규칙 아래 놓는다.

### git conflict가 이미 막고 있는 것 (중복 구현 금지)

- 두 브랜치가 동시에 `qa_run/v9/system.md` 추가 → 같은 경로 add/add conflict. 무조건 뜬다.
- default 버전이 조용히 바뀌는 것 → `test_the_default_qa_version_is_v8`가 새 버전
  디렉토리가 생기는 순간 깨진다. `resolve_version`이 `versions[-1]`을 쓰므로.
- frontmatter/placeholder drift → `test_the_shipped_prompts_all_pass_validation`이
  실제 트리에 `validate_prompts()`를 돌린다.

## Non-goals

- **프롬프트 내부의 의미적 모순 탐지.** "always X"와 "never X"가 한 파일에 공존하는 걸
  자동으로 잡는 건 LLM 판정이 필요하고, 판정이 흔들리면 CI가 flaky해진다. 리뷰 몫.
- **프롬프트 ↔ 툴 description 교차 검증.** ARTEL-192 이후 사용 정책이 툴 description에
  살고 있어서 양쪽이 어긋날 수 있지만, 어긋남의 정의가 케이스마다 다르다.
  `test_v3_shortens_...`처럼 **쌍이 정해진 것만** 수동으로 핀 박는 현재 방식을 유지한다.
- **git merge driver / `.gitattributes`.** lock 파일 conflict는 재생성으로 푸는 게
  맞고, 자동 병합은 오히려 검토를 건너뛰게 만든다.
- **다른 레포(orchestration, sdk)로 확장.** 프롬프트가 여기에만 있다.
- **버전 immutability를 런타임에 강제하는 것.** 부팅 시점엔 이미 늦었고,
  `validate_prompts()`는 파일이 *유효한지* 보지 *변했는지*는 못 본다.

## Context / Constraints

**CI 실체를 먼저 확정해야 한다.** `Jenkinsfile`의 Test 스테이지는 이것뿐이다:

```
sh 'docker build --target test -t $TEST_IMAGE_TAG .'
```

그리고 `Dockerfile`의 test 타깃은:

```dockerfile
FROM base AS test
COPY app ./app
COPY tests ./tests
RUN pip install --no-cache-dir -e ".[dev]"
RUN python -m pytest
```

여기서 나오는 제약이 설계를 거의 결정한다:

| 제약 | 결과 |
|---|---|
| 컨테이너 안에 **git 히스토리가 없다** | `main`과 diff 뜨는 방식 불가. 비교 대상이 **커밋된 데이터**여야 한다 |
| `COPY`는 `app`, `tests`, `pyproject.toml`, `README.md`만 | lock 파일이 레포 루트에 있으면 컨테이너에 안 들어간다. `skills-lock.json` 위치를 따라하면 안 됨 |
| Test 스테이지에 `when` 가드 없음 | PR 빌드에서도 돈다. Jenkinsfile 수정 불필요 |
| 검사가 pytest 안에서 끝남 | 파이프라인 변경 0, 새 의존성 0 |

→ **lock 파일 방식이 이 제약에 정확히 맞는다.** 커밋된 JSON 하나를 디스크 상태와
비교하는 순수 pytest. Jenkinsfile도 Dockerfile도 안 건드린다.

**해시는 이미 있다.** `PromptFile.body_sha256` (`loader.py`) — frontmatter를 제외한
body만의 sha256이고, 제외 이유가 코드 주석에 적혀 있다("`note` is documentation —
changing it does not change what the model read"). 새로 정의하지 말고 이걸 쓴다.
부작용: 릴리즈된 버전의 `note`만 고치는 건 통과한다. **의도된 동작**이다.

현재 대상: `app/prompts` 아래 `.md` 25개
(qa_run v1–v8 ×2 role, scenario v1–v2 ×2, game_context v1 ×2,
knowledge_query v1 ×2, qa_compaction v1 ×1).

**지금이 착수 적기.** `.worktrees/*`의 진행 중 브랜치(ARTEL-241/242/246/267/270)를
확인한 결과 **develop보다 앞선 프롬프트 버전을 추가한 브랜치가 없다**(전부 v8 이하).
즉 지금 넣으면 in-flight 브랜치에 rebase 마찰이 생기지 않는다.

## Approach (Checklist)

- [ ] **Step 0: Recon** — 완료. 위 Context에 반영.
  - `app/prompts/loader.py` — `PromptFile.body_sha256`, `known_agents()`,
    `available_versions()`, `_read_prompt()` 재사용 지점
  - `Dockerfile` / `Jenkinsfile` — COPY 범위와 git 부재
  - `pyproject.toml` — `[tool.setuptools.package-data]`가 `"**/*.md"`만 포함

- [ ] **Step 1: `app/prompts/lock.py`**
  - `compute_lock() -> dict` — `known_agents()` × `available_versions()` × `roles_in()`을
    순회해 `{"version": 1, "prompts": {"<agent>/<version>/<role>": "<sha256>"}}` 생성.
    키 정렬. 공개 API인 `load_prompt(agent, role, version)`을 써서 파싱 규칙이 한 군데만
    있게 한다 (private `_read_prompt`가 아니라 — plan review 지적).
  - **`roles_in(agent, version)`을 `loader.py`에 뺀다.** 지금
    `validate_prompts()` 안에 `sorted(path.stem for path in directory.glob("*.md"))`로
    인라인돼 있고, `compute_lock()`이 같은 걸 또 써야 한다. "무엇이 role 파일인가"가
    두 군데로 갈리면 잠금 대상과 검증 대상이 조용히 어긋난다.
  - `compute_lock()`은 **Settings에 의존하지 않는다.** `resolve_version`은
    `version`이 명시되면 `_configured_version()`을 타지 않으므로
    (`chosen = version if version is not None else ...`), `*_PROMPT_VERSION`
    환경변수가 없는 맨 컨테이너에서도 동작한다. 이게 깨지면 CI가 환경에 따라 흔들린다.
  - `read_lock() -> dict` — `LOCK_PATH`(= `PROMPTS_ROOT / "prompts-lock.json"`) 로드.
    파일 없으면 `PromptError`.
  - `python -m app.prompts.lock --write` — 재생성 진입점.
    `json.dumps(indent=2, sort_keys=True, ensure_ascii=False)` + 개행 하나.
  - 위치를 `app/prompts/` 안에 두는 이유: 잠그는 대상 옆에 있어야 하고,
    `COPY app` 범위 안이어야 컨테이너에서 읽힌다. `known_agents()`는
    `child.is_dir()`로 필터하므로 `.json` 파일이 agent로 오인되지 않는다 — 확인 완료.
  - `package-data`에 `*.json`은 **추가하지 않는다**. 런타임에서 읽는 코드가 없고,
    wheel에 들어갈 이유가 없다. test 타깃은 `pip install -e`라 소스 트리를 그대로 본다.

- [ ] **Step 2: `app/prompts/prompts-lock.json` 베이스라인 생성**
  - `python -m app.prompts.lock --write`를 현재 HEAD에서 1회 실행해 커밋.
  - 이 시점의 lock은 정의상 통과한다 — 도입 자체는 no-op.

- [ ] **Step 3: `tests/test_prompts_lock.py`** — 실패 모드를 셋으로 나눠서, 에러 메시지가
  "무엇을 하라"를 말하게 한다. 한 테스트로 묶으면 어떤 상황인지 로그에서 구분이 안 된다.
  - `test_a_released_prompt_body_never_changes` — lock과 디스크 양쪽에 있는 키의 해시 불일치.
    메시지: *"v7을 고치지 말고 새 버전을 만들어라. 과거 런이 v7로 기록돼 있다."*
  - `test_every_prompt_on_disk_is_in_the_lock` — 디스크에만 있는 키(신규 버전, 또는
    기존 버전에 role 파일 추가). 메시지: *"`python -m app.prompts.lock --write` 실행 후 커밋."*
  - `test_the_lock_never_loses_a_prompt` — lock에만 있는 키. 릴리즈된 프롬프트 삭제 차단.
  - 비교는 **파싱된 dict** 기준. 포맷 차이로 빨개지지 않게 한다.

- [ ] **Step 4: 문서**
  - `loader.py` 모듈 docstring에 lock 한 문단 추가 — 불변 규칙이 이미 거기 적혀 있으니
    강제 수단도 같은 자리에.
  - `lock.py` 모듈 docstring에 재생성 명령과 **conflict는 손이 아니라 `--write`로 푼다**는
    규칙.
  - `.agents/docs/project.md`의 Commands 표 추가는 **이번 브랜치에서 하지 않는다.**
    작업 트리에 이 이슈와 무관한 미커밋 변경(`## API 표면과 신뢰 경계` 절)이 이미
    올라와 있어, 같이 스테이징하면 `commit.md`의 "Do not mix unrelated behavior"를
    어긴다. 그 변경이 머지된 뒤 한 줄 추가로 후속 처리한다.

## Validation

- **Commands to run:**
  ```bash
  python -m pytest tests/test_prompts_lock.py tests/test_prompts_loader.py tests/test_qa_prompt_version.py
  ```
  ```bash
  docker build --target test -t artel-agent-server:lock-check .
  ```
- **음성 대조 (반드시 수행, 결과 보고에 포함):**
  1. **`app/prompts/qa_run/v4/vision_directive.md`** 끝에 문단 하나 추가 →
     `test_a_released_prompt_body_never_changes` **하나만** red 확인 → 되돌린다.
     대상 선정이 이 대조의 전부다. 위 커버리지 표에서 **보호 전무** 칸에 있는 파일이어야
     "기존 테스트는 통과하는데 lock만 잡는다"가 증명된다. 처음 초안이 골랐던
     `v7/system.md`는 **틀렸다** — `test_v8_is_v7_and_marks_the_tool_set_that_changed_under_it`의
     `assert v8 == v7`이 같이 터져서 무엇이 잡았는지 구분이 안 된다 (plan review 지적).
  2. `app/prompts/qa_run/v9/`를 v8 복사로 만들고 → `test_every_prompt_on_disk_is_in_the_lock`
     red 확인 → 지운다. (`test_the_default_qa_version_is_v8`도 같이 터진다 — 예상된 동작이고,
     이쪽은 **의도적으로** 중복 신호다)
  3. `app/prompts/qa_run/v2/vision_directive.md`를 **디스크에서** 치운다 →
     `test_the_lock_never_loses_a_prompt` red 확인 → 되돌린다.
     초안은 "lock 엔트리 하나 삭제"라고 썼는데 **틀렸다** — 그건 디스크 ⊃ lock이라
     대조 2와 같은 테스트가 잡는다. 이 테스트를 태우려면 방향이 반대여야 한다.
     대상은 다른 테스트가 참조하지 않는 파일이어야 하고, `v2/vision_directive`가 그렇다.
- **Expected output:** 정상 트리에서 전부 green. 대조 1은 새 테스트 하나만 red.
- **의도적으로 검증하지 않는 것:** 프롬프트 내용의 의미적 정합성. Non-goals 참조.

## Risks & Rollback

- **Risks:**
  - *lock 파일이 merge conflict 핫스팟이 된다.* 두 브랜치가 각각 버전을 추가하면
    같은 JSON 근처를 건드린다. **의도된 마찰**이다 — 두 버전이 동시에 들어온다는 사실이
    리뷰에 보인다. 규칙: **conflict는 손으로 풀지 말고 `--write` 재실행으로 푼다.**
    손으로 풀면 잘못된 해시를 커밋해 잠금이 무의미해진다. Step 4 문서에 명시할 것.
  - *새 버전을 낼 때마다 한 단계가 는다.* 프롬프트 버전 추가는 드물고(8개월간 qa_run 8개),
    이미 `test_the_default_qa_version_is_v8` 갱신이 필요한 작업이라 실질 증가는 미미하다.
  - *`note`만 수정하면 통과한다.* `body_sha256` 정의상 그렇다. 의도된 동작이며,
    v8이 v7과 body가 같고 note로만 구분되는 실제 사례
    (`test_v8_is_v7_and_marks_the_tool_set_that_changed_under_it`)가 이 선택을 뒷받침한다.
  - *릴리즈 전 버전을 다듬는 중인 브랜치.* 아직 머지 안 된 새 버전을 반복 수정하면
    매번 `--write`가 필요하다. 마찰이 실제로 크면 **develop에 없는 버전은 잠그지 않는 예외**를
    나중에 고려한다 — 다만 컨테이너에 git이 없어 develop을 알 방법이 없으므로, 그때는
    Jenkinsfile에 별도 스테이지가 필요하다. **지금은 하지 않는다.**

- **Rollback steps:** `app/prompts/lock.py`, `app/prompts/prompts-lock.json`,
  `tests/test_prompts_lock.py` 삭제. 다른 코드에 진입점이 없어 부작용 없음.
  단일 커밋으로 유지해 `git revert` 한 번에 끝나게 한다.

## Plan Review

`.agents/skills/plan-review` 프로토콜. 서브에이전트 미사용(세션 제약) → 스킬이 명시한
폴백대로 fast / medium / heavy 역할을 순차 자체 수행.

**must-fix (반영됨)**

- 음성 대조 대상이 틀렸다. `v7/system.md`는 `v8 == v7` 어서션에 걸려 있어 두 테스트가
  동시에 터진다 → 보호 전무 파일인 `v4/vision_directive.md`로 교체.
- 구멍 크기가 플랜에 없었다 → 25개 중 10개 무보호라는 실측 표 추가. PR 본문 근거.
- private `_read_prompt` 사용 → 공개 `load_prompt(agent, role, version)`로 교체.

**should-fix (반영됨)**

- `validate_prompts()`의 role 탐색 인라인과 `compute_lock()`이 같은 로직을 두 번 갖는다
  → `roles_in()` 추출.
- `compute_lock()`의 Settings 비의존성이 암묵적이었다 → 근거를 플랜에 명시.

**확인 후 무해 판정**

- `test_prompts_loader.py`의 `prompt_root` fixture가 `PROMPTS_ROOT`를 tmp로 갈아끼우지만
  `monkeypatch` 복원 + teardown `clear_prompt_cache()`가 있어 lock 테스트로 새지 않는다.
  다만 `lock.py`는 `from ... import PROMPTS_ROOT`로 이름을 고정 바인딩하지 말고
  호출 시점에 `loader.PROMPTS_ROOT`를 읽는다.

**Rejected feedback**

- *`"version": 1` 봉투는 YAGNI다 → 빼라.* 거부. 같은 레포의 `skills-lock.json`이 이미
  같은 모양이고, 키 하나 값이다. 일관성이 더 싸다.
- *세 테스트를 하나로 합쳐라.* 거부. 셋의 조치가 각각 다르고(새 버전 만들기 / `--write` /
  삭제 되돌리기), 합치면 CI 로그만 보고 무엇을 할지 알 수 없다.

**heavy 판정: PASS** — 범위 3파일 + docstring, 검증이 실패를 실제로 재현하고,
되돌리기가 단일 커밋 revert.

## Open Questions

- Jira 이슈를 새로 딸지, 프롬프트 버저닝 계열(ARTEL-238 라인)에 붙일지.
- `qa_compaction`은 `SETTINGS_VERSION_KEYS`에 있는데 `test_prompts_loader.py`의
  `StubSettings`에는 `qa_compaction_prompt_version` 필드가 없다. 이 계획 범위 밖이지만
  작업 중 눈에 띈 불일치 — 별건으로 확인 필요.
