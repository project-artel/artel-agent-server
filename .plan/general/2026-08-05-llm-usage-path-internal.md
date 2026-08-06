# 2026-08-05 — LLM 사용량 전송 경로를 /internal/llm-usage 로 변경

- Date: 2026-08-05
- Jira: ARTEL-267
- Status: Reviewed (fast NONPASS→revised, medium PASS, heavy PASS)

## Goal

`app/llm/usage.py`의 `USAGE_PATH` 상수를 `/api/orchestration/llm-usage`에서
`/internal/llm-usage`로 바꾼다. ARTEL-265(orchestration의 내부 서버-투-서버
경로를 `/internal/**`로 통일)를 따라가는 변경이다.

## Non-goals

- 전송 실패 시 재시도·영구 버퍼 도입 (현재의 손실 허용 설계 유지).
- 다른 orchestration 호출 경로 정리 (확인된 호출부는 `USAGE_PATH` 하나뿐).
- `ORCHESTRATION_BASE_URL` 변경 (포트 분리는 ARTEL-266, .env 변경이지 코드 변경 아님).
- 배치 전송 동작, 페이로드 형태, 버퍼 정책(flush_size, max_buffer 등) 변경.

## Context / Constraints

- ARTEL-265가 먼저 배포되어야 `/internal/llm-usage` 경로가 존재한다. 배포
  순서가 반대면 이 티켓 배포 직후부터 404. 이 리포의 코드 변경 자체는
  ARTEL-265 배포 여부와 무관하게 지금 준비 가능 (별도 에이전트가 동시에
  orchestration 쪽 ARTEL-265 진행 중).
- 두 배포 사이 창에서는 어느 순서든 사용량 전송이 유실된다. 호환 alias
  없음. 이슈에서 허용한 손실이며, PR 본문에 배포 순서 의존성을 명시해야 함.
- `USAGE_PATH`를 참조하는 곳은 `app/llm/usage.py` 자신
  (`_post`, 208~213줄)뿐. 리포 전체 검색(`grep -rn "orchestration/llm-usage\|USAGE_PATH"`)
  결과 다른 참조 없음.
- `tests/test_llm_usage.py`는 경로 문자열을 하드코딩하지 않는다
  (`base_url="http://orchestration.test"`만 사용, path assertion 없음).
  즉 오늘 기준 "갱신해야 할 기존 테스트"는 없다 — 대신 AC 3번째 항목
  ("테스트가 새 경로를 검증한다")을 만족시키려면 새 경로를 검증하는
  테스트를 추가해야 한다.
- 모듈 docstring/주변 주석은 "Orchestration"이라고만 하지 특정 경로
  문자열을 언급하지 않는다 — 고칠 주석 없음 (확인 필요, Step 0에서 재확인).

## Approach (Checklist)
- [ ] **Step 0: Recon** — `app/llm/usage.py`와 `tests/test_llm_usage.py`를
      다시 읽고 37번 줄 주변 주석, docstring에 옛 경로 문자열이 없는지
      최종 확인.
- [ ] **Step 1: Implementation** — `app/llm/usage.py:37`의
      `USAGE_PATH = "/api/orchestration/llm-usage"`를
      `USAGE_PATH = "/internal/llm-usage"`로 변경. 그 외 코드 변경 없음.
- [ ] **Step 2: Tests** — `tests/test_llm_usage.py`에 신규 테스트 함수
      `test_post_sends_to_the_internal_llm_usage_path`를 추가한다.
      리포에는 `respx`/`pytest-httpx` 같은 HTTP 목킹 의존성이 없음을
      Recon에서 확인함 (`pyproject.toml`에 `httpx`만 있음). 대신
      `unittest.mock.patch("httpx.AsyncClient.post", new=AsyncMock(...))`로
      `UsageBuffer._post`(기본 send 경로, `send=` 주입 없이 실제 경로
      구성 로직을 태움)가 실제로 호출하는 URL을 가로채
      `"http://orchestration.test/internal/llm-usage"`와 정확히 일치하는지
      단언한다. 기존 테스트는 전부 `send=` 주입을 써서 `_post`/`USAGE_PATH`
      조립 자체를 태우지 않으므로, 이것은 갱신이 아니라 신규 추가이자
      이 리포에서 유일하게 실경로 조립을 검증하는 테스트다.
- [ ] **Step 3: PR 본문 작성** — PR 여는 사람이 본문에 배포 순서
      의존성을 명시적으로 적는다: "ARTEL-265가 먼저 배포되어야 하며,
      반대 순서면 배포 직후 404. 두 배포 사이 창에서는 순서와 무관하게
      사용량 전송이 유실되며 이는 허용된 손실." 이 문장이 빠진 채로
      PR을 열지 않는다.
- [ ] **Step 4: Rollout / Rollback** — 코드 변경 자체는 되돌리기 쉬움
      (`git revert`). 실제 배포는 ARTEL-265가 먼저 나가야 하며, PR 본문에
      이 순서를 명시한다(Step 3). 롤백은 이 리포만으로 완결되지 않음 —
      ARTEL-265가 먼저 롤백되지 않는 한 이 변경만 되돌리면 다시 옛
      경로(404)로 돌아간다.

## Validation
- **Commands to run:** `.agents/docs/testing.md` 지시대로 저장소 테스트
  스위트 실행 (`python -m pytest`, 필요 시 프로젝트 지정 명령으로 교체).
- **Expected output:** 전체 테스트 통과, 신규/갱신된 테스트가 새 경로를
  검증함.

## Risks & Rollback
- **Risks:** 배포 순서(ARTEL-265 먼저)를 어기면 배포 직후 404, 손실
  허용 구간 발생 — 코드 변경으로는 막을 수 없는 운영 리스크이므로 PR
  본문에 명시하는 것으로 대응.
- **Rollback steps:** `git revert`로 상수만 되돌림. 단, ARTEL-265가
  이미 배포된 상태라면 되돌리는 순간 다시 404 창이 열린다는 점을 PR에
  남긴다.

## Open Questions
- 없음. AC/Constraints/Non-goals가 Jira 이슈에 이미 명확히 적혀 있음.

## Rejected Feedback

- Fast reviewer가 `_post`에서 `self._base_url`이 `None`인 채로 호출되면
  `f"None/internal/llm-usage"`가 된다는 엣지 케이스를 지적했다. 실제로는
  `UsageBuffer.add()`/`flush()` 둘 다 `self.enabled`
  (`base_url is not None`)를 먼저 확인하고 아니면 즉시 반환하므로
  `_post`는 `base_url`이 설정된 경우에만 도달한다. 이 가드는 기존 코드에
  이미 있고 이번 변경과 무관하며, 상수 값만 바뀌는 이 작업의 범위 밖이라
  플랜에 반영하지 않는다.
