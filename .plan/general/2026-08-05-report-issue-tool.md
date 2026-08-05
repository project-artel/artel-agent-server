# 2026-08-05 — QA 에이전트의 report_issue 툴

- Date: 2026-08-05
- Jira: ARTEL-246 (Epic ARTEL-11 [Backend] Agent 서버 개발)
- Branch: `feat/qa-에이전트가-발견한-버그를-report-issue-툴로-보고하게-한다-ARTEL-246`
- Status: Draft

## Goal

QA 에이전트가 게임에서 발견한 결함을 **구조화된 이슈**로 보고하게 한다. Orchestration은
`ISSUE` 프레임을 이미 받아 `issue` 테이블에 저장할 준비가 되어 있고, 지금 비어 있는 것은
그 프레임을 싣는 이쪽 절반뿐이다.

짝이 되는 계획: artel-orchestration-server `.plan/general/2026-08-05-issue-read-and-resolve-api.md`
(ARTEL-245, 저장·조회), artel-home `.plan/general/2026-08-05-qa-issue-console.md`(ARTEL-247, 화면).

## Non-goals

- 이슈 중복 판정·병합. 같은 버그를 두 번 보고해도 그대로 두 행이 된다. 접는 일은 사람이 화면에서
  한다.
- 스크린샷을 이슈에 첨부하는 별도 업로드 경로. `capture_screen`이 이미 URL을 남기므로 이슈 본문이
  그 순간을 가리키면 충분하다.
- 이슈 수정·철회 툴. 잘못 보고한 이슈는 화면에서 처리한다.
- 기존 프롬프트 버전(v1~v4) 문구 변경.

## Context / Constraints

Orchestration이 요구하는 것(`QaAgentInboundRouter`):

- 허용 타입 집합에 `"ISSUE"`가 이미 있다.
- `payload.title`이 비어 있지 않아야 한다 — 다른 타입의 `payload.message` 자리를 이슈는
  `title`로 쓴다.
- `payload.severity`가 `BLOCKER|CRITICAL|MAJOR|MINOR|TRIVIAL` 중 하나여야 한다. 아니면 프레임은
  ORCHE_INTERNAL 에러로 **버려진다**(런은 계속된다).
- `payload` 전체가 `issue.detail`(JSONB, 1 MiB 상한)에 그대로 저장된다.
- `messageId`로 멱등하다. 재전송은 중복 행을 만들지 않는다.
- **응답 프레임이 없다.** KNOWLEDGE_CREATE/DELETE와 같은 단방향이므로 이쪽은 절대 기다리지 않는다.

이쪽 현재 상태:

| 위치 | 지금 |
|---|---|
| `app/qa/envelope.py` | `MessageType`에 ISSUE **없음**. severity 열거도 없음 |
| `app/agents/qa/tools.py` | 20개 툴. 이슈 계열 **없음** |
| `app/prompts/qa_run/` | v1~**v6**(v5 컨텍스트 압축, v6 스텝 실패 처리). 이슈 보고 언급 **없음** |

프롬프트 관례: 각 버전의 frontmatter가 "이전 문구는 그대로 두고 한 문단만 추가한다"라고 적었듯,
**새 버전은 이전 버전 복사 + 문단 추가**다. 기본값은 최신 버전이며 `qa_prompt_version`으로 핀할 수
있다. v1은 `test_prompts_v1_regression.py`가 문자 단위로 고정한다. 최신이 **v6**이므로 신규는 v7.

## 계약

```python
class IssueSeverity(StrEnum):
    BLOCKER, CRITICAL, MAJOR, MINOR, TRIVIAL

class IssuePayload(BaseModel):
    title: str            # Orchestration의 표시용 필드. 한 줄 요약
    severity: IssueSeverity
    step: int | None      # 어느 시나리오 스텝에서 관측했나
    expected: str         # 무엇이 일어나야 했나
    actual: str           # 무엇이 일어났나
    reproduction: list[str]   # 재현 절차, 첫 줄부터 순서대로
```

`expected`/`actual`/`reproduction`은 Orchestration이 검증하지 않지만 `detail`에 그대로 실린다.
이 셋이 없으면 이슈는 "뭔가 이상했다"는 문장 하나로 남는다 — 툴이 받아야 필드가 채워진다.

## Approach (Checklist)

- [ ] **Step 0: Recon** — `QaAgentInboundRouter.routeIssue`의 요구 필드, `envelope.py`의 단방향
      프레임 주석(KNOWLEDGE_CREATE), `tools.py`의 캡 관례(`MAX_CAPTURES_PER_RUN`), 프롬프트
      버저닝 관례 확인. *(완료)*

- [ ] **Step 1: 봉투** — `app/qa/envelope.py`
      - `MessageType.ISSUE` 추가. 주석에 **단방향**임과 severity가 Orchestration의 사다리와
        글자까지 같아야 함을 적는다.
      - `IssueSeverity(StrEnum)` 5값, `IssuePayload` 신설.

- [ ] **Step 2: 캡과 툴** — `app/agents/qa/arch.py`, `app/agents/qa/tools.py`
      - 런당 허용치는 **arch가 소유한다**. `QaArchSpec`/`ResolvedArch`에
        `max_issues_per_run`(기본 10, `ge=0, le=50` — 형제 필드와 같은 범위)을 더하고,
        `REPORT_ISSUE_DESCRIPTION`은 그 값으로 포맷한다. arch.py가 적어둔 대로 허용치가 툴 옆
        상수로 있으면 fingerprint가 보지 못해 실행 설정 비교에서 빠진다. `CAPTURE_SCREEN_DESCRIPTION`도
        모듈 상수가 아니라 `arch.max_captures_per_run`으로 포맷된다.
      - `ResolvedArch.tool_call_limit()`의 base에는 **더하지 않는다**. 캡처와 같은 취급이며 이유도
        같다 — 그 함수의 주석대로 base에 넣으면 "이슈를 한 번도 보고하지 않는 런까지 포함해 모든
        런의 상한이 조용히 넓어진다". 지식 검색·기록은 시나리오와 무관한 조회라 base에 들어가지만,
        이슈 보고는 **지금 판정 중인 스텝에 대한 기록**이라 그 스텝의 예산(`tool_calls_per_step`,
        기본 15/스텝)에서 나가는 것이 맞다. 기본 10이면 최악의 경우에도 한 스텝분에 못 미친다.
        덕분에 `test_the_default_budget_matches_what_runs_had_before`가 그대로 통과한다 —
        기존 런의 예산은 한 칸도 움직이지 않는다.
      - `QaRunState.issues_reported` 카운터(시도 기준 — 캡이 실패 경로에서도 물려야 한다,
        `captures_attempted`와 같은 이유).
      - `report_issue(step, severity, title, expected, actual, reproduction, thought)`
        - `thought`를 타임라인에 남기고(`channel.note`), `channel.emit(MessageType.ISSUE, ...)`.
        - severity 문자열은 `IssueSeverity`로 검증하고, 틀리면 **툴 결과로 되돌려** 고쳐 부르게
          한다. 그냥 보내면 Orchestration이 조용히 버려 에이전트는 보고했다고 착각한다.
        - 캡 초과 시 프레임을 보내지 않고 남은 수를 알리는 문자열을 돌려준다.
      - `tools` 목록에 등록.

- [ ] **Step 3: 프롬프트** — `app/prompts/qa_run/v7/`
      - **파일 두 개를 복사한다**: `system.md`와 `vision_directive.md`. 버전 디렉터리마다 둘 다
        있고, `run_config.py`와 `runner.py`가 vision이 켜진 런에서 **해당 버전의**
        `vision_directive.md`를 읽는다. 하나만 만들면 v7이 곧 기본값이 되므로 vision 가능한
        모델의 모든 런이 세션 열기에서 `PromptError`로 죽는다. `vision_directive.md`는 v6에서
        글자 그대로 복사한다.
      - `system.md`는 v6 복사 + 한 문단. 담을 내용: `report_step`은 **스텝의 판정**이고
        `report_issue`는 **게임의 결함**이라는 구분, 실패한 스텝이 곧 이슈는 아니라는 점
        (시나리오가 틀렸을 수도 있다), severity 고르는 기준, 재현 절차를 쓰라는 지시.
      - frontmatter `version: v7`, `note`에 무엇을 더했는지.
      - v1~v6은 건드리지 않는다.

- [ ] **Step 4: 테스트** — `tests/test_qa_tools.py`
      - `report_issue`가 `ISSUE` 프레임을 `payload.title`/`payload.severity`와 함께 보낸다
      - severity 오타면 **프레임을 하나도 보내지 않고**(emit 호출 자체가 없음) 오류 문자열을
        돌려준다. 이미 유효한 보고를 한 번 한 뒤에도 같아야 하므로, 유효 → 무효 순서로 부른
        경우까지 확인한다
      - 캡 초과가 프레임을 보내지 않는다
      - `thought`가 타임라인에 남는다
      - **기존 테스트 세 곳이 새 버전·새 툴을 명시적으로 못 박고 있어 함께 고쳐야 한다**:
        - `tests/test_qa_tools.py::test_the_agent_is_offered_exactly_these_tools` —
          집합에 `"report_issue"` 추가
        - `tests/test_qa_prompt_version.py::test_the_default_qa_version_is_v6` —
          v7로 고치고 이름도 함께 바꾼다(기본값은 최신을 따라가지만 그 수치는 테스트가 못 박는다)
        - 같은 파일의 버전별 내용 테스트 관례(`test_v6_says_...`)를 따라 v7 문단을 못 박는
          테스트를 더한다. v6의 각 문단이 v7에 그대로 남아 있는지 확인하는 초집합 검사도 포함한다
          — "이전 문구는 그대로 둔다"는 관례를 지켰다는 증거가 그것뿐이다
      - `tests/test_qa_arch.py`: `max_issues_per_run`이 spec의 범위 검증과 fingerprint에
        잡히는지, 그리고 **`tool_call_limit`을 움직이지 않는지**를 확인하는 케이스를 더한다.
        `test_the_default_budget_matches_what_runs_had_before`는 손대지 않는다 — 그 테스트가
        그대로 통과하는 것이 base를 건드리지 않았다는 증거다

- [ ] **Step 5: Rollout** — 배포 순서는 **Orchestration → Agent**. 반대로 가면 프레임이
      저장은 되지만(테이블은 이미 있다) 조회 API가 없어 확인할 길이 없다. 롤백은 프롬프트를
      `qa_prompt_version=v6`으로 핀하면 툴 호출이 사실상 멈춘다 — 코드 롤백 없이 되돌릴 수 있다.

## Validation

- **Commands to run:** `python -m pytest tests/test_qa_tools.py tests/test_prompts_loader.py
  tests/test_qa_prompt_version.py tests/test_prompts_v1_regression.py`, 이후 `python -m pytest`
- **Expected output:** 전부 통과. v1 회귀 테스트가 통과해야 기존 프롬프트를 건드리지 않았다는
  증거가 된다.

## Risks & Rollback

- **Risks**
  - 프롬프트가 이슈 보고를 권할수록 에이전트가 사소한 것까지 올릴 수 있다. severity 기준을
    문단에 명시하고 캡으로 막되, 실제 분포는 첫 런들을 보고 조정한다.
  - `ISSUE` 프레임은 Orchestration이 조용히 버릴 수 있는 유일한 툴 경로다(응답이 없으므로).
    이쪽에서 severity를 미리 검증하는 것이 그 침묵을 줄이는 유일한 수단이다.
- **Rollback steps:** `qa_prompt_version=v4` 핀 → 툴은 남아 있으나 호출되지 않는다. 완전
  제거가 필요하면 `git revert`.

## Rejected feedback

- **캡을 `tool_call_limit()` base에 더하자**(heavy 2차). 더하지 않는다. 기본 20을 base에 넣으면
  고정 예산이 `23 + 15n`에서 `43 + 15n`으로 87% 넓어지고, 그 대가를 이슈를 한 번도 보고하지 않는
  런까지 전부 치른다. 대신 기본을 10으로 낮추고 스텝 예산 안에서 쓰게 두었다(캡처와 같은 취급).
  지적의 핵심인 "예산 영향이 계획에 드러나지 않았다"는 이 결정으로 해소된다 — 영향이 0이다.

## Open Questions

- 캡 10이 적정한지는 실사용 전에는 모른다. 한 런이 10개를 채우는 일이 잦으면 시나리오가 너무
  넓거나 게임이 실제로 그만큼 깨진 것이므로, 그때 arch 기본값을 다시 본다(코드가 아니라 knob이라
  실행 설정으로 바꿀 수 있다).
