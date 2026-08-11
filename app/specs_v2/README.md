# specs_v2 — 복합 근거 연결형 명세 발견기

SDK JSON 하나를 typed evidence graph로 분석하는 결정론적 명세 발견 모듈이다.

## 핵심 차이

SDK record를 곧바로 Spec으로 만들지 않는다.

1. `objects`, `components.calls`, `refs`, `types/unplaced`, `callPath`, `condition`, `effects`를 typed evidence graph로 보존한다.
2. 화면 control/input에서 순방향으로, observable/availability effect에서 역방향으로 탐색한다.
3. 같은 path에서 만난 결과는 원자 `Contract`로 만든다.
4. 같은 외부 entry와 조건에 연결된 contract/state는 `Scenario`로 구성한다.
5. 다른 조건 arm은 합치지 않고 `BranchFamily`가 contract id로 참조한다.
6. Editor/DevBuild는 입력별로 완전히 독립 분석하고, 각각 별도의 명세와 검토 목록을 생성한다.

모든 assertion은 `entry_id`, `method_id`, IL `offset`을 보존한다. 모호한 대상·값·경로는 자연어로 감추지 않고 `candidate`, `review` 또는 `unsupported`로 구분한다.
근거 품질과 별도로 `actionability`, `observability`, `applicability` 세 실행 축을 유지한다.

`candidate`는 Ready와 Review 사이의 보수적인 중간 등급이다. scene·trigger·assertion target은 확정됐고 화면에서 관찰 가능하지만, 표현식 기대값 또는 안전하게 제한된 호출 조건 합성 근거가 덜 닫힌 경우만 허용한다. `unresolved_target`, scene 미확정, runtime instance 미관찰, 읽지 못한 조건은 candidate로 승격하지 않는다. 생성 연산은 생성 대상이 구조적으로 확정되면 별도의 반환값 없이도 완성된 기대 결과로 판정한다.

Candidate는 Ready Specs에서 `status=candidate`와 남은 `review_reason`을 함께 표시한다. 완전히 증명된 상태 연결만 유지하기 위해 candidate 행은 Connected Flows 합성에는 참여하지 않는다.

## 현재 구현한 연결 motif

- scene control → UnityEvent wiring → entry method → call path → effect
- input/gesture → guarded path → effect
- component field → inspector ref → scene object/asset
- scene object active snapshot + control wiring → UI 기본 표시 상태 확인
- unplaced type → createdBy owner placement
- 한 path의 여러 observable effect → multi-assertion contract
- 같은 trigger/feature의 서로 다른 condition → branch family
- 같은 entry/condition의 여러 contract와 supporting state → connected scenario
- 선행 call의 persisted write → 후행 PlayerPrefs read → branch pruning
- active-scene condition → capture scene applicability pruning
- 조건부 비활성화 + 같은 control click guard → 실행 불가능한 click arm 제거
- delegate handoff 입력 → 같은 state-machine의 resume 이후 조건·효과
- resume 이후 증감 상태 → 입력 전 사전 조건으로 역투영
- coroutine state field → 선언 타입과 원래 coroutine 이름으로 범위 구분
- Editor/Player semantic contract reconciliation

## 실행

Orchestration Server는 Editor 또는 개발 빌드 SDK JSON을 각각 별도 요청으로
전송한다. 서버는 다른 capture나 저장된 상태를 합치지 않는다.

```http
POST /internal/specs/v2/generate
Content-Type: application/json

{ ...raw SDK JSON... }
```

응답은 `ready_specs`, `review_specs`, `connected_flows`와 각 개수를 포함한다.
요청 경로에서 파일, 데이터베이스, LLM을 사용하지 않는다.

Editor 명세를 생성할 때 DevBuild 결과를 읽지 않으며, DevBuild 명세도 Editor 결과를 읽지 않는다. 한쪽에만 있는 기능 역시 해당 capture의 근거가 충분하면 그쪽 명세에 포함된다.
SDK 원본 provenance인 `capture=player`는 보존하되, `development=true`인 Player 산출물은 사람이 구분하기 쉽도록 명세의 `artifact` 열에서 `devbuild`로 표시한다.

CSV의 선두 열은 `precondition | test_step | expected_result`다. 화면 lifecycle effect가 UI 활성/비활성 결과라면 `화면 진입 후 관찰`로 남기지 않고 대상별 `표시 상태 확인` 스텝으로 투영한다. 코드가 active 상태를 바꾸는 control에는 raw snapshot을 중복 적용하지 않고 조건부 코드 근거를 우선한다. 입력 gesture는 테스트 스텝으로 승격한 뒤 사전 조건에서 제거하며, residual guard와 결과가 같은 OR arm만 대안 입력으로 합친다.
행은 SDK 산출물의 `scenes` 순서로 화면별 그룹을 만들고, 같은 `flow_id`의 초기화·전이 행은 Ready 안에서 연속 배치한다. `flow_role`, `state_before`, `state_after`, `flow_id` 열이 연결 관계를 명시한다.

사람용 사전 조건의 `X 화면인 상태`와 같은 AND 문맥에 `SceneManager.GetActiveScene().name == "X"`가 있으면 active-scene equality를 화면 상태 표현에 흡수하고 나머지 조건만 출력한다. 다른 scene과의 equality는 흡수하지 않으며, OR arm 안의 equality도 그 arm이 선택됐다고 단정할 수 없으므로 보존한다. scene placement와 Unity active Scene의 원시 근거는 구별하고 contract JSON의 조건은 변경하지 않는다.

합성 flow는 `scene + state write target + observable target/operation`이 같고 초기화와 입력 전이가 모두 증명되는 경우에만 생성한다. 생성된 연결 메타데이터는 Ready 행에 통합되며 `.flows.csv`는 같은 행의 전용 조회 뷰다.

증거 없는 derived `runtime_event`가 같은 scene에서 동일 assertion/source와 supporting state를 만들고, 그 호출 경로가 더 긴 도달 가능 경로의 suffix이면 Ready에서는 상위 실행 경로가 이를 포괄한다. control/input/pointer는 residual condition이 같을 때만 포괄하며, 간접 runtime caller가 guard를 가지고 하위 callee가 `always`인 경우에는 callee의 독립 실행 증거가 없는 한 caller guard를 실제 문맥으로 보존한다. 간접 행은 중복 노출하지 않고 `covered_spec_ids`와 `contract_ids`로 이를 증명하는 모든 상위 실행 행에 provenance를 합친다. 원자 contract/scenario는 JSON에서 그대로 보존한다.

`WaitUntil`처럼 입력 predicate가 delegate로 분리된 coroutine은 `handedOverAt`, 동일 entry/state-machine, branch/call offset을 연결해 resume 명세를 만든다. resume 이후 `i = i + 1` 같은 상태 갱신이 있으면 후행 분기 조건을 `(i + 1) ...` 형태의 입력 전 사전 조건으로 투영한다. 정확한 UI 값이 SDK에서 모호하면 입력 연결은 보존하되 해당 행은 Review에 남긴다.

사람이 읽는 사전 조건에서는 SDK 타입 근거로 정수임이 확인되고 같은 경계에 대한 정확한 `+1` 또는 `-1` 갱신이 증명될 때만 단위 경계식을 정규화한다. 예를 들어 `x < N`과 `(x + 1) >= N`은 `x == (N - 1)`로, `x < N`과 `(x + 1) < N`은 `x < (N - 1)`로 표현한다. 이 단계는 산술 관계만 증명하며 `마지막 스토리` 같은 도메인 의미나 서로 다른 명세 사이의 연결 의미를 부여하지 않는다. 원자 contract와 합성 전 조건 근거는 JSON에 보존한다.

화면 진입·지속 관찰·표시 상태 확인의 결과는 현재 상태를 나타내는 문장(`있다`, `상태다`, `출력되어 있다`)으로 렌더링하고, 입력·클릭 등 동작의 결과는 변화 문장을 유지한다.

`Start` 또는 scene 배치가 확인된 `Awake` 자체에서 transition/quit assertion이 직접 발생하면 lifecycle 자동 동작으로 투영한다. 이 경우 실행할 scene은 사전 조건이 아니라 테스트 스텝의 `X 화면에 진입한다`로 옮기고, 남은 guard만 사전 조건에 둔다. 결과는 `별도 입력 없이 자동 전환된다`처럼 표현하되 exact frame은 추정하지 않는다. assertion source가 coroutine `MoveNext`나 다른 method이면 직접 자동 동작으로 승격하지 않으며, `OnEnable`은 scene 진입 대신 대상 활성화 문맥으로 표현한다.

사람용 입력 표기에서는 SDK의 키 phase를 동작명으로 중복 노출하지 않는다. 예를 들어 원본 `control=Space, phase=down`은 근거 JSON에 그대로 남기되 명세에는 `Space 입력`으로 쓴다. 입력으로 재개된 코루틴이 아닌 내부 코루틴 결과는 `Start1` 같은 구현 메서드명을 테스트 스텝으로 쓰지 않고, 해당 화면에 머무르며 진행 결과를 관찰하는 단계로 표현한다.

UI 대상은 안정적인 hierarchy path를 기본 식별자로 유지한다. SDK object가 정적인 control caption/label 또는 sprite를 제공하면 실행 문장에 섞지 않고 각각 `ui_text`, `ui_sprite` 열에 `hierarchy path = value`로 기록한다. 값이 없거나 대상을 하나로 확정할 수 없으면 추측하지 않는다.

증거 없는 내부 runtime entry가 외부 control/input/pointer entry와 서로 다른 호출 경로를 사용해도 동일한 effect method/offset에 합류하고, scene·capture·assertion·supporting state가 같으며 callee 조건이 caller 조건에 포함됨을 증명할 수 있으면 외부 실행 행이 내부 행을 포괄한다. component receiver가 caller field로 재결합된 경우에는 연산자·우변과 owner 이후 property tail이 모두 같은 조건만 동일한 것으로 인정한다.

외부 경로가 내부 runtime entry의 전체 호출 경로를 suffix로 포함하는 경우, 내부 supporting state가 외부 scenario에 직접 복사되지 않았더라도 그 state의 owner type과 method가 외부의 모든 call path에 실제 포함되면 실행이 증명된 것으로 본다. 이때 내부 행은 Ready에서 포괄하고 supporting state와 provenance를 이를 증명하는 모든 외부 행으로 승계한다. 동명 method 오인을 막기 위해 owner type과 method를 함께 비교한다. Ready와 connected flow는 같은 projected supporting-state 집합을 사용한다.

같은 trigger/entry/condition으로 scenario를 구성할 때 부가 state path는 정렬상 첫 contract가 아니라 그룹의 모든 contract call path와 호환성을 검사한다. 따라서 contract 입력 순서가 달라져도 supporting state가 달라지지 않는다.

충돌 명세의 테스트 스텝은 `OnTriggerEnter2D`만 표시하지 않고 call path 첫 method의 owner type을 함께 기록한다. 따라서 동일한 최종 효과로 이어지는 서로 다른 외부 충돌 원인은 별도 행으로 유지하면서 사람이 구분할 수 있다.

Animator의 `SetTrigger`, `ResetTrigger`, `Play`, `CrossFade`, `CrossFadeInFixedTime` 호출은 상태명/파라미터명과 나머지 인수가 literal일 때 exact 기대값으로 취급한다. 충돌 callback 자체는 trigger로 보존하지만 런타임 생성 인스턴스의 scene placement나 target binding이 없으면 해당 명세는 Review에 남긴다.

## 테스트

```bash
python -m pytest tests/test_specs_v2_api.py
```

## 의도적인 한계

- SDK가 문자열로 직렬화한 expression을 완전하게 재파싱하지 않는다.
- 이름 유사성만으로 read/write 또는 target을 연결하지 않는다.
- `alsoReachedBy`의 공유 condition은 다시 계산할 수 없으므로 review로 둔다.
- audio는 현재 화면 기반 QA capability에서 unsupported다.
- cross-method 인과는 call path, call order, persisted key처럼 구조로 증명되는 경우만 연결한다.
