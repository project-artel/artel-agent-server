# 2026-08-18 — 씬 명세만으로 기능을 잇는 능력을 측정한다

- Date: 2026-08-18
- Jira: ARTEL-457
- GitHub Issue: None
- Status: Draft

## Goal

같은 근거를 세 형태(content_map JSON / 의사 C# 렌더 / 둘 다)로 주고, 모델이 **기능 사이의
상태 의존(체인)** 을 얼마나 옳게 잇는지 재는 **측정 하네스**를 만든다. 손으로 만든 골든 체인
10개 대비 **정답률 · 지어냄 비율 · 과소연결**과 소요시간 · 비용을 arm 별로 낸다. 결과로 TC
agent 의 입력 형식을 정한다.

## Non-goals

- 프로덕션 에이전트 동작 변경. `app/` 아래는 **읽기만** 한다. 이 이슈는 측정이다.
- content_map 적재기, `wv2cs.py`, 골든 content_map 재생성. 셋 다 이미 있고 입력으로 받는다.
- 실재하는 두 기능을 근거 없이 이은 것의 자동 판정. 기계로 못 잡는다 — 리포트에 남긴다.
- LangSmith·usage 적재. 하네스는 비용을 응답에서 직접 읽고 파일로 남긴다.

## Context / Constraints

기준은 `origin/develop`.

### 입력 (이미 준비됨, 재생성하지 않는다)

| 경로 | 무엇 |
|---|---|
| `.parallel-inputs/wv-editor-latest.json` | 실제 WordVenture 근거 캡처 (1.4 MB, schema 6) |
| `.parallel-inputs/golden-content-map.json` | 같은 캡처의 손으로 쓴 content_map (7 씬 / 18 기능) |
| `.parallel-inputs/wv2cs.py` | 캡처 → 의사 C# 렌더러. arm (b) 입력을 만든다 |

캡처(1.4 MB)와 렌더러는 이 레포 밖에 산다. **경로를 CLI 인자로 받고** 실행 결과에 각
입력의 `sha256` 을 적는다. 골든 content_map(30 KB)만 답안지이므로 레포에 싣는다.

### 확인한 사실 — 실험 설계를 바꾸는 것들

1. **arm (a) 와 arm (b) 의 근거는 같지 않다.** 의사 C# 은 content_map 이 버린 것을 들고
   있다. 예: `GameClearController.ShowGettedCard` 가
   `(MapMove.StagePosition - 1) == StageDataSingleton.stagePosition` 을 읽고,
   `GameClearController.Start` 가 `StageDataSingleton.stagePosition == 4` 로
   `EndingScene` 을 연다. content_map 의 어느 기능에도 없다. 반대로 의사 C# 에는
   `capabilityId` · `status` · `selector` · `verification` 이 없다.
   → 채점을 content_map 한 곳에만 걸면 arm (b) 가 부당하게 "지어냄"으로 떨어진다.
   **인용 검증을 두 단으로 나눈다**(아래 "인용 검증").
2. **arm (b) 는 `capabilityId` 를 댈 수 없다.** 인용 스키마에 `unit`
   (`"Map.MapMove.CharacterMove"`)을 나란히 두고, 하네스가 `evidence.ownerType` +
   `evidence.method` 로 기능 행을 되찾는다. 한 메서드가 여러 기능으로 갈린 곳
   (`MapMove.CharacterMove` → 10·11·12)은 후보 집합으로 풀고, 골든과 겹치면 맞은 것으로
   센다. **arm (b) 에 유리한 비대칭이며 리포트에 명시한다.**
3. `MapMove.position`(레인 인덱스)과 `MapMove.StagePosition`(스테이지 진행도)은 다른 것이다.
   캡처에서 `MapMove.WordPosition` 이 `MapMove.position = stagePosition` 을 쓰기까지 한다 —
   이름 기반 오분류의 실물이다. 골든 음성 체인 하나를 여기에 건다.
4. content_map 안에서 `StageDataSingleton.stagePosition` 을 **읽는 기능은 없다**(기능 13이
   쓰기만 한다). 이슈 본문의 예시(771 writes / 812 reads)가 바로 이 모양이라, 모델이 읽는
   쪽을 지어낼 자리다.

### 기계 조인 (과소연결의 기준선)

기능 행의 `then[].target` 과 다른 기능 행의 `given` 안 식별자를 문자열로 맞춘 것.
현재 골든 맵에서 **22쌍**이 나온다.

**과소연결의 분모는 22가 아니라 7이다.** 22쌍을 `(쓰는 메서드, 읽는 메서드, 상태)` 로 접으면
서로 다른 사실 **7개**가 남는다. `MapMove.CharacterMove` 하나가 기능 행 셋(10·11·12)으로
갈려 있어 `{10,11,12}²` 만으로 6쌍이 나오는데, 그것은 전부 같은 한 문장이다.
쌍으로 세면 `unit` 으로 인용하는 arm 이 한 체인으로 6쌍을 덮고, `capabilityId` 로 정확히
인용하는 arm 은 5번 놓친 것으로 기록된다 — 정밀한 쪽이 벌을 받는다. 접어서 센다.

## 골든 체인 10개

체인 = "기능 A 가 상태 X 를 쓰고, 기능 B 가 X 를 읽어 관측 가능한 결과를 낸다".
7개는 **근거가 받치는 것**(에이전트가 내야 맞다), 3개는 **지어내기 쉬운데 근거가 없는 것**
(에이전트가 안 내야 맞다).

**골든을 전부 기계 조인 안에서 고르면 안 된다.** 조인만 돌린 답이 만점을 받아 버리면
정답률이 모델에 대해 아무것도 말하지 않는다. supported 7 중 5개는 조인 안에서,
**2개는 조인이 닿지 못하는 곳에서** 고른다.

### 조인이 닿는 것 (supported, in-map)

| id | writer → reader | via | 관측 |
|---|---|---|---|
| SC-1 | 23 → 24 | `BattleWaveController.wave` | `GameClearScene` 진입 |
| SC-2 | 24 → 10 | `MapMove.StagePosition` | 캐릭터가 `battle1` 로 이동 |
| SC-3 | 10 → 12 | `MapMove.position` | 캐릭터가 `village` 로 복귀 (레인 인덱스이지 진행도가 아니다) |
| SC-4 | 30 → 31 | `GameClearController.flag` | `Map_scene` 진입 |
| SC-5 | 21 → 22 | `CombineButton.combineZone` | combineZone 꺼짐 |

### 조인이 닿지 못하는 것 (supported, 캡처에만 있음)

읽는 쪽이 content_map 의 어느 기능 행에도 없다. 캡처 — 곧 의사 C# 렌더 — 에만 있다.
**arm (a) 는 원리적으로 이 둘을 낼 수 없고, arm (b)/(c) 는 낼 수 있다.**
이 두 칸이 없으면 arm (b) 의 이득을 잴 자리가 아예 없어 입력 형식 권고가 미리 정해진다.

| id | writer → reader | via | 관측 |
|---|---|---|---|
| SC-6 | 24 → `Scenes.GameClearController.ShowGettedCard` | `MapMove.StagePosition` | 어느 보상 카드를 `Instantiate` 하는지가 갈린다 |
| SC-7 | 13 → `Scenes.GameClearController.Start` | `StageDataSingleton.stagePosition` | `EndingScene` 진입 |

읽는 쪽 끝은 `capabilityId` 가 없고 `unit`(`Owner.Method`)으로만 지목된다.

### 근거가 없는 것 (unsupported / 함정)

| id | 주장 | 왜 지어내기 쉬운가 | 무엇이 없나 |
|---|---|---|---|
| SC-8 | 13 → 20 via `StageDataSingleton.stagePosition` | "고른 스테이지가 전투에 실린다"는 어느 게임에서나 그럴듯하다. 이슈 예시와 모양이 같고, **그 상태를 읽는 쪽이 실제로 있기까지 하다** — 다만 `GameClearController` 이지 `TurnBattleSystem` 이 아니다 | `TurnBattleSystem` 은 `currentTurn`·`PlayerTurn` 만 읽는다. 맵에도 캡처에도 없다 |
| SC-9 | 24 → 12 via `MapMove.position` | `StagePosition` 과 `position` 을 같은 것으로 본다. 캡처의 `MapMove.WordPosition` 이 `MapMove.position = stagePosition` 을 쓰기까지 해서 더 헷갈린다 | 24 는 `StagePosition` 만 쓴다. 서로 다른 변수다 |
| SC-10 | 31 → 10 via `MapMove.StagePosition` | "맵으로 돌아가면 클리어 표시가 켜진다" | 31 은 씬만 연다. 올리는 것은 24 다. `GameClearController` 는 읽기만 한다 |

### 뺀 것

- **22 → 21 (combineZone 역방향)**: `unit` 으로 인용하면 SC-5 와 **완전히 같은 인용 쌍**이
  된다. 한 체인이 두 골든을 동시에 맞히므로 독립된 신호가 아니다.
- **1 → 10 (StagePosition 0 이 이동을 막는 gating)**: 조인 안에 있어 SC-2 와 겹치는 자리를 잰다.

### 귀무가설 — 모델 없이 조인만 돌린 답

`join_baseline_checks` 가 조인 22쌍을 그대로 체인으로 내면 **8/10**, 지어냄 0%, 과소연결 0 이다.
못 맞히는 것은 SC-6·SC-7 둘뿐이다. **이 수치를 arm 셋과 나란히 싣는다.** 8/10 을 넘지 못하고
지어냄이 0 보다 큰 arm 은 grep 보다 나은 것이 없다는 뜻이고, 그것이 이 실험이 답해야 할 질문이다.

## 인용 검증 (기계 대조)

### 출력 스키마 — arm 셋이 같다

인용 하나는 항상 네 칸이고, **어느 arm 이든 같은 스키마**다. 다른 것은 어느 칸을 채울 수
있느냐뿐이다. content_map 을 받은 arm 은 `capabilityId` 를 대고, 의사 C# 만 받은 arm 은
그것을 알 길이 없으니 `unit` 을 댄다. 채우지 못하는 칸은 `null` 로 둔다.

```json
{ "chains": [ { "summary": "...",
                "chain": [ { "capabilityId": 23, "unit": null, "role": "writes",
                             "via": "BattleWaveController.wave" },
                           { "capabilityId": 24, "unit": null, "role": "reads",
                             "via": "BattleWaveController.wave" } ] } ] }
```

| arm | 채우는 칸 | 예 |
|---|---|---|
| (a) content_map 만 | `capabilityId` | `{"capabilityId": 23, "unit": null, ...}` |
| (b) 의사 C# 만 | `unit` | `{"capabilityId": null, "unit": "Combat.Enemies.BattleWaveController.WaveEndSensor", ...}` |
| (c) 둘 다 | `capabilityId` (있으면 `unit` 도) | `{"capabilityId": 23, "unit": "…WaveEndSensor", ...}` |

`role` 은 `writes` 또는 `reads`. 스키마 위반(빠진 칸, 모르는 `role`)은 그 체인을 버린다.

### 기능 행 되찾기 (resolution) — 한 가지 원시 연산

인용 → 기능 행 집합을 돌려주는 함수 하나만 둔다. **인용 검증과 골든 적중 판정이 같은
함수를 쓴다** — 두 곳이 "이 인용이 가리키는 기능 행"을 서로 다르게 세면 수치가 뜻을 잃는다.

- `capabilityId` 가 있으면 → 그 행 하나. **없는 id 는 지어냄이다**(`unit` 으로 구제하지 않는다).
- 없고 `unit` 이 있으면 → `evidence.ownerType` + `evidence.method` 가 맞는 행 **전부**.
  네임스페이스는 떼고 뒤 두 마디로 맞춘다(`Map.MapMove.CharacterMove` = `MapMove.CharacterMove`).
  코루틴 상태 기계(`<WaveEndSensor>d__6::MoveNext`)는 `WaveEndSensor` 로 되돌린다.
  실제 후보 집합:

  | unit | 기능 행 |
  |---|---|
  | `MapMove.CharacterMove` | 10, 11, 12 |
  | `BattleWaveController.WaveEndSensor` | 23, 24 |
  | `GameClearController.Update` | 30, 31, **40** (40 은 GameOverScene — 후보가 씬을 넘는다) |
  | `TitleSceneManager.InitPlayerData` | 1, 2 |
  | `CombineButton.OnButtonClick` | 21, 22 |

- **`unit` 이 캡처에는 있는데 맵에 행이 없으면 → 빈 집합.** 이것은 지어냄이 아니다.
  골든 적중으로 세지 못할 뿐이고, `via` 대조는 아래 `in-capture` 단으로 넘어간다.
  진짜 코드를 인용한 arm 을 지어냄으로 세면 arm (b) 의 수치가 통째로 거짓이 된다.
- 둘 다 없으면 → 빈 집합.

**적중 판정**: 골든의 한쪽 끝은 `capabilityId` 로도 `unit` 으로도 지목될 수 있다
(SC-6·SC-7 의 읽는 쪽은 맵에 행이 없어 `unit` 뿐이다). 인용 하나가 그 끝을 가리킨다는 것은
`골든.capabilityId ∈ resolve(인용)` 이거나 `normalize(인용.unit) == normalize(골든.unit)` 이라는 뜻이다.
체인 하나가 골든 `(W, R, via)` 를 냈다는 것은 **그 체인 안에** `writes` 인용이 `W` 를,
`reads` 인용이 `R` 를 가리키고, **둘 다의 `via` 가 골든의 상태를 가리킬 때**다.
`via` 를 빼면 안 된다 — arm (b) 에서 SC-2(24→10 via `StagePosition`)와
SC-9(24→12 via `position`)는 양 끝이 같은 메서드로 풀려, `via` 없이는 한 체인이
정답과 함정을 동시에 맞히는 물건이 된다.

**arm (b) 에 유리한 비대칭이며 수치와 함께 밝힌다** — 후보 집합이 넓을수록 맞히기 쉽고
동시에 지어냄으로 떨어지기도 어렵다.

### `via` 대조

인용 하나의 판정은 셋 중 하나다.

- `in-map` — 되찾은 기능 행 **중 하나라도** `via` 를 갖는다. `writes` 면 `then[].target`,
  `reads` 면 `given` 안 점 찍힌 식별자와 맞춘다. **통과.**
- `in-capture` — 맵에서는 못 찾았지만 캡처 레코드(`Owner.Method`)에 있다.
  **역할을 지킨다**: `writes` 는 `effects[].target` 에서만, `reads` 는 `condition` 에서만 찾는다.
  둘을 OR 로 묶으면 `ShowGettedCard` 가 `MapMove.StagePosition` 을 **쓴다**는 주장이
  통과해 버리는데, 그것이 바로 SC-10 의 모양이다. **통과로 세되 따로 집계한다.**
- `unverified` — 어디에도 없다. **버린다. 지어냄이다.**

맞춤은 정확 일치이거나 한쪽이 다른 쪽의 멤버다
(`CombineButton.combineZone` 을 쓴 것과 `CombineButton.combineZone.activeSelf` 를 읽은 것은
같은 상태다). 둘 다 `통과` 이므로 우선순위를 정할 필요가 없다.
점이 없는 이름(`stagePosition`)은 어느 타입의 것인지 정해지지 않아 상태로 세지 않는다.

**체인 하나는 상태 하나다.** 인용이 전부 통과해도, 그 인용들이 **서로 다른 상태**를
가리키면 체인은 떨어진다. `WaveEndSensor writes MapMove.StagePosition` 과
`CharacterMove reads MapMove.position` 은 각자 참이고 묶은 것만 거짓인데, 인용별 대조로는
절대 잡히지 않는다 — 이름 기반 오분류가 정확히 그 모양이다. 규칙은 프롬프트에도 적는다
(여러 상태를 거치는 이야기는 체인 여럿으로 내라).

기계가 못 잡는 실패 하나 — **실재하는 두 기능을, 같은 상태를 대면서, 인과 없이 이은 것** —
은 남는다. SC-8·SC-9·SC-10 은 셋 다 대조에서 떨어지도록 골랐으므로 이 하네스가 잡지만,
"양쪽 행에 같은 `via` 가 진짜 있는데 인과가 없는" 조합은 QA 런만 판정할 수 있다.

## 측정 지표

한 번의 실행(run) = arm 하나 × 반복 하나. 지표는 run 마다 내고, arm 별로 **평균과 최소–최대**를
함께 적는다. 반복 2회는 분산을 보이기 위한 것이지 평균을 정밀하게 만들기 위한 것이 아니다.

| 지표 | 정의 |
|---|---|
| 정답률 | 골든 10 중 맞은 수 / 10. supported 는 **냈고 인용이 전부 통과**하면 맞음, unsupported 는 **안 냈으면** 맞음(통과 여부와 무관하게, 냈으면 틀림) |
| 지어냄 비율 | `unverified` 인용을 하나라도 가진 체인 수 / 그 run 이 낸 전체 체인 수. arm 수치는 반복분을 **합쳐서**(pooled) 낸다 |
| 상태 섞임 | 인용은 전부 통과했는데 한 체인이 두 상태를 뒤섞은 수. 지어냄에 합치지 않는다 — 근거를 못 댄 것과 잘못 묶은 것은 다른 실패다 |
| 과소연결 | 조인 링크 **7개** 중 에이전트가 닿지 못한 수. 링크는 22쌍을 `(쓰는 메서드, 읽는 메서드, 상태)` 로 접은 것이다 |
| in-capture 인용 | 맵에는 없고 캡처에만 있는 사실을 인용한 수. arm (b)/(c) 가 맵이 버린 것을 주웠는지 |
| 소요시간 | run 당 벽시계 초 |
| 비용 | OpenRouter 가 청구한 `cost`(USD) + 입출력 토큰 |

3차 스프린트 TC Agent 평가 틀(골든 케이스 생성률 · 소요시간 · 비용)과 같은 축이다.

수치표에는 **arm 셋 + 조인 귀무가설** 네 줄을 싣는다. 귀무가설은 8/10 · 지어냄 0% ·
과소연결 0 · 비용 0 이고, `outOfMapCorrect` 만 0/2 다. arm 하나가 이보다 나은 곳은
**딱 거기 하나**다 — 맵 밖 사실을 주웠는가.

**정답률을 과하게 읽지 않는다.** SC-1·SC-4·SC-5 는 쓰는 쪽과 읽는 쪽이 같은 메서드라
arm (b) 에서는 "한 메서드를 두 번 대는" 모양이 된다. 10개 중 양 끝이 서로 다른 메서드인
것은 SC-2·SC-3(같은 메서드)·SC-6·SC-7 뿐이다. 표본이 작다는 뜻이고, 작은 차이로 결론을
내지 않는다.

## 결정 — 코드 자리

레포에 평가/실험 코드 관례가 **없다**(`scripts/` 는 로컬 러너, `app/` 은 제품, `tests/` 는 평면).
새로 하나 만든다:

```
evals/scene_chain/
  __init__.py
  evidence.py    content_map 기능 행 색인 + 캡처 레코드 색인 + 기계 조인
  citations.py   인용 해석(capabilityId | unit) + in-map/in-capture/unverified 판정
  scoring.py     골든 로더 + 정답률/지어냄/과소연결/시간/비용
  arms.py        arm 별 입력·프롬프트 조립
  run.py         CLI 러너
  prompts/system.md, prompts/human.md
  data/golden-content-map.json   (답안지, 출처 주석 포함)
  data/golden-chains.json        (손으로 쓴 10개)
  output/                        (gitignore)
tests/test_scene_chain_eval.py
```

`app/prompts/` 에 넣지 않는다 — 거기는 부팅에서 검증되고 lock 이 걸린 제품 프롬프트
레지스트리다. 실험 프롬프트가 섞이면 제품 계약이 흔들린다.

프롬프트는 **두 장**이다. `system.md` 는 arm 과 무관하고, `human.md` 는 근거 블록만
arm 별로 갈아 끼우는 틀 하나다. arm 마다 프롬프트를 따로 쓰면 프롬프트 차이와 입력 형식
차이가 섞여 무엇을 잰 것인지 말할 수 없게 된다. 실제로 보낸 본문 전체는 결과 파일에 싣는다.

### Rejected feedback

- **"`content_map.py` 와 `capture.py` 를 나눠 두라"** (medium 리뷰). 이유가 "맵만 쓰는
  테스트가 1.4 MB 캡처를 안 읽게"였는데, 두 로더가 한 모듈에 있어도 읽기는 `Capture.load()`
  를 부를 때 일어난다. 모듈을 나눠도 그 성질이 더 생기지 않아 파일만 늘어난다. 한 모듈에 둔다.
- 같은 이유로 `chains.py`(기계 조인)는 `evidence.py` 안에, `golden.py`(순수 로더)는
  `scoring.py` 안에 둔다. 골든 로더에는 채점 로직을 넣지 않는다 — 읽어서 dataclass 로 주는
  것까지가 전부다.

## Approach (Checklist)

- [ ] **Step 1 — 읽기 계층.** `evidence.py`. 기능 행에서 writes(=`then[].target`)와
      reads(=`given` 안 점 찍힌 식별자)를 뽑는다. 캡처는 `types`·`unplaced` 의 레코드를
      `Owner.Method` 로 색인하고 `effects[].target` / `condition` 을 모은다.
      시그니처를 못 읽는 레코드는 건너뛰고, 같은 메서드의 여러 레코드는 합집합으로 합친다.
      기계 조인도 여기 둔다.
- [ ] **Step 2 — 인용 검증.** `citations.py`. 위 "기능 행 되찾기"와 "`via` 대조" 그대로.
- [ ] **Step 3 — 골든 데이터.** `data/golden-chains.json` 10건 + `scoring.py` 안의 순수 로더.
- [ ] **Step 4 — arm 조립.** `arms.py`. (a) content_map JSON, (b) 의사 C# 파일들을 파일명
      헤더와 함께 이어 붙인 하나, (c) 둘 다. 렌더된 의사 C# 디렉터리 경로를 인자로 받는다
      (`wv2cs.py` 를 이 하네스가 부르지 않는다 — 렌더러는 레포 밖 입력이다).
- [ ] **Step 5 — 채점.** `scoring.py`. 위 지표 표대로.
- [ ] **Step 6 — 러너.** `run.py`.
      - `app.llm.chat_model.build_chat_model` 을 그대로 쓴다. 온도는 그 모듈의 고정값 0.2 이고
        결과에 그대로 적는다. OpenRouter 경유 `ChatOpenAI` 에 seed 를 넣을 자리가 없어
        결정론은 못 만든다 — 반복 간 분산으로 대신한다.
      - 인자: `--content-map` `--capture` `--pseudo-cs` `--arm` `--model` `--repeats`.
        **입력 경로가 없거나 못 읽으면 즉시 죽는다**(exit 2). 절반 채워진 측정이 제일 나쁘다.
      - `--replay <응답.json>` 은 모델을 부르지 않고 그 파일의 응답으로 채점만 다시 돌린다.
        경로를 명시로 받는다 — 파일명 규칙으로 찾아내면 어느 응답을 채점했는지 나중에 못 댄다.
      - `output/<utc>-<arm>-<n>.json` 에 프롬프트 본문·입력 sha256·원문 응답·인용 판정·지표를 남긴다.
- [ ] **Step 7 — 테스트.** `tests/test_scene_chain_eval.py`, 네트워크 없음:
      - 인용 해석(id / unit / 없는 id)과 판정 3분기
      - 기계 조인 쌍 집합 고정 (전체 22, 서로 다른 두 기능 14)
      - 채점 산식 (supported 적중 / unsupported 회피 / 지어냄 / 과소연결)
      - **골든 무결성**: supported 7은 조인에 있고 인용 검증을 통과해야 하고,
        unsupported 3은 인용 검증에서 떨어져야 한다. 답안지가 틀리면 여기서 깨진다.
        이것은 테스트의 부수 효과가 아니라 **의도한 게이트**다 — 답안지를 손으로 고칠 때
        조용히 틀리는 것을 막는 것이 제일 중요하다.
- [ ] **Step 8 — 실행.** arm 3개 × 반복 2회. 수치와 권고를 이슈·PR 에 남긴다.

## Risks

- **모델 예산.** 남은 크레딧 $6.09. arm (b)/(c) 프롬프트가 각각 ~45k/~55k 토큰이다.
  sonnet 5 기준 3 arm × 2 반복 ≈ $1 미만으로 잡는다. 초과하면 반복을 1회로 줄이고 그렇게 적는다.
- **비대칭 채점.** arm (b) 의 `unit` 해석이 후보 집합을 관대하게 받는다. 수치와 함께 밝힌다.
- **표본 1개.** 게임 하나, 캡처 하나, 골든 10개다. arm 간 차이가 작으면 결론을 내지 않는다.
- **온도 0.2.** 결정론이 아니다. seed 를 노출하는 경로가 없어 반복 간 분산으로 대신한다.

## Rollback

`evals/` 디렉터리와 테스트 파일 하나가 전부다. 제품 경로를 건드리지 않으므로 되돌리기는 삭제다.
