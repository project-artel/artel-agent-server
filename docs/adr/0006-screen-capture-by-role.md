# ADR 0006 — screen capture 를 역할별로 2~4 장 싣는다

- 상태: 확정, 아직 `develop` 에 없음
- 근거: commit `7d060a6` (`feat: screen capture 를 역할별로 2~4 장 싣는 by_role mode 를 더한다`),
  `8b2e1e4`, `d1d9d7e`, `f8806dc`
- **plan 문서가 없습니다.** 이유가 적힌 곳은 commit message 뿐입니다 (ARTEL-868, ARTEL-870).
- ARTEL-870 branch 는 ARTEL-868 branch 위에 서 있고 둘 다 아직 `develop` 에 없습니다.
  아래에 나오는 `app/agents/qa/arch.py` 와 `app/agents/qa/vision.py` 의 코드는
  **그 branch 의 것**입니다. `develop` 에는 `screen_capture` 축 자체가 없습니다.

## 결정

- `QaArchSpec.screen_capture` 축의 값이 셋입니다 — `on_demand`, `every_call`, `by_role`.
  기본값은 `on_demand` 라 오늘의 런은 달라지지 않습니다.
- `by_role` 은 상황에 따라 **둘에서 넷**을 싣습니다.
- 역할은 **기계적으로** 정합니다. 모델이 고르지 않습니다.
- 그림은 여전히 대화에 저장하지 않습니다 (ADR 0005).

## 왜

### 1. 한 장은 무엇이 달라졌는지 말하지 못합니다

- `every_call` 은 매 호출 현재 화면 한 장만 싣습니다. 그 한 장은 지금 무엇이 있는지는
  말하지만 무엇이 달라졌는지는 말하지 못합니다.
- ARTEL-868 파일럿이 그 한계를 숫자로 보여 줬습니다. L1 의 실패 기대 스텝 여섯에 대해
  `correct_fail` 이 **baseline 0 과 2, 그림 arm 1 과 1** 입니다 (arm 마다 런 둘).
- 매 호출 그림을 실어도 여섯 중 다섯을 놓칩니다.
- QA 판정의 상당수가 「눌렀더니 바뀌었나」이고, 그것은 두 장을 나란히 놓아야 보입니다.

### 2. 역할 넷과 무엇이 그것을 옮기나

| 역할 | 언제 옮기나 | 언제 내려가나 |
| --- | --- | --- |
| `pre_action` | `state.last_action_frame` 이 움직였을 때, 지난 턴의 `current` 가 옮겨 옴 | 다음 action 까지 |
| `checkpoint` | scene 이름이 바뀌었을 때 | 그 scene 에 있는 동안 유지 |
| `failure` | `step_results` 에 실패가 하나 늘었을 때 | `FAILURE_CAPTURE_TURNS` 인 6 턴 뒤 |
| `current` | 매 호출 | 다음 호출에 갈림 |

- `failure` 가 몇 턴을 버티는 이유는 실패가 그 턴에 판정되지 않는 일이 많기 때문입니다.
  agent 는 다시 눌러 보고 다른 길을 찾아본 뒤 실패로 적습니다. 그 사이 화면이 사라지면
  판정할 때 근거가 없습니다. 반대로 오래 들고 있으면 지나간 실패가 계속 넉 장을 채웁니다.
- 6 은 그 사이에서 고른 값이고, 파일럿이 다른 수를 말하면 바꿉니다.

### 3. 역할을 모델이 고르면 비교가 성립하지 않습니다

- 모델이 고르게 하면 arm 이 **두 가지로** 달라집니다 — 받는 그림과, 그 그림에 대해
  내리는 판단.
- 그러면 비교가 축 하나에 대한 것이 아니게 됩니다.
- 그래서 `_shift_roles` 의 모든 트리거가 런이 만든 값에 대한 비교뿐입니다.
  이 함수에 모델 입력이 닿지 않습니다.

### 4. `current` 가 맨 뒤입니다

- 오래된 것부터 싣고 `current` 를 마지막에 둡니다.
- 마지막에 읽은 것이 지금 화면이어야 모델이 무엇을 기준으로 판단할지 헷갈리지 않습니다.
- `target_id` 로 잘라 온 그림은 역할 caption 뒤에 자기 caption 을 붙입니다.
  어느 요소인지가 그 그림의 절반이라, 역할 caption 이 그것을 덮으면 모델은 자기가
  요청한 close-up 을 전체 화면으로 읽습니다.

### 5. 내려받은 그림을 들고 갑니다

- `HeldCapture` 는 URL 이 아니라 인코딩된 바이트를 들고 있습니다. 이유가 둘입니다.
  - **같은 그림이 여러 턴 실립니다** — `checkpoint` 는 scene 내내, `failure` 는 6 턴,
    `pre_action` 은 지난 턴의 `current` 를 그대로 물려받습니다. 턴마다 다시 받으면
    왕복이 턴 수만큼 늘어납니다.
  - **다운로드 URL 이 30 분짜리입니다.** orchestration 의
    `StorageProperties.captureDownloadUrlTtl` 이 `Duration.ofMinutes(30)` 입니다.
    긴 런의 후반에는 만료됩니다.
- 만료는 storage 가 내는 평범한 403 으로 옵니다. 만료라고 이름을 말하는 것이 없습니다.

## 거절한 대안

| 대안 | 왜 거절했나 |
| --- | --- |
| 한 장만 계속 싣는 `every_call` | L1 실패 기대 스텝 여섯 중 다섯을 놓침 |
| 어느 그림이 중요한지 모델이 고르게 함 | arm 이 두 축으로 달라져 비교가 한 축에 대한 것이 아니게 됨 |
| `current` 를 맨 앞에 싣기 | 마지막에 읽은 것이 지금 화면이 아니면 판단 기준이 흐려짐 |
| 역할이 바뀔 때마다 URL 로 다시 받기 | 왕복이 턴 수만큼 늘고, 30 분짜리 URL 이 긴 런 후반에 만료됨 |
| `by_role` 을 기본값으로 | 기본은 `on_demand` 로 두어 오늘의 런이 한 줄도 안 달라지게 함 |
| 그림을 대화에 저장 | 역할이 턴마다 바뀌므로 저장하면 매 턴 전사를 고쳐 쓰게 되고, $0.87 대 $14.22 가 그대로 돌아옴 |

## 대가

| 대가 | 무엇이 일어나나 |
| --- | --- |
| 한 호출에 그림 넷까지 실림 | input token 이 그만큼 늘고, 그 넷은 전부 cache 밖임 |
| **`arch_fingerprint` 가 두 구현을 못 가름** | `screen_capture` 값이 같은 두 build — 저장하던 것과 요청에만 싣는 것 — 이 같은 digest 를 받음. `QA_ARCH_LABEL` 이 유일한 구분 |
| 역할 트리거가 SDK 필드에 기댐 | `last_action_frame` 을 모르는 옛 SDK 에서는 `pre_action` 이 영영 안 생김 |
| `FAILURE_CAPTURE_TURNS` 가 근거 없는 6 | 파일럿이 다른 수를 말하면 바꿀 값 |
| 파일럿이 arm 당 런 둘 | 모델은 표집이라 같은 설정도 매번 다른 스텝 수를 냄. 둘은 결론이 아니라 신호 |
| 숫자의 출처가 commit message 뿐 | 이 저장소에 결과 파일도 런 로그도 없음 |

## label 셋과 fingerprint 셋이 안 맞는 이유

- `arch_fingerprint` 가 해시하는 것은 scheme, knob(`arch.model_dump()` 에서 `label` 제외),
  tool 이름과 인자 스키마, middleware 이름의 순서입니다.
- `by_role` 은 **fingerprint 로 갈립니다** — `screen_capture` 가 세 번째 문자열을 갖기
  때문입니다.
- 저장하던 `every_call` 과 요청에만 싣는 `every_call` 은 **안 갈립니다.**
  knob 도 tool 도 middleware 이름도 그대로이고, 달라진 것은 middleware **안쪽**입니다.
- 그래서 arm 이 넷인데 digest 는 셋입니다.

| label | `screen_capture` | fingerprint 로 갈리나 |
| --- | --- | --- |
| `v4-capture-on-demand` | `on_demand` | 예 |
| `v4-capture-every-call` (은퇴) | `every_call` | 아니오 |
| `v4-capture-transient` | `every_call` | 아니오 |
| `v4-capture-by-role` | `by_role` | 예 |
