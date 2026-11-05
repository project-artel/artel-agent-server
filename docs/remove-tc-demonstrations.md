# TC 시연 기능 제거

2026-10-28 사용자 요청으로 사람이 직접 게임을 조작해 검증 절차를 알려주던 기능을 제거했다. QA agent가 수행한 실행 영상, TC 판정 검토와 사람 피드백은 유지한다.

- `/internal/demonstrations/extract`와 추출 agent·schema·prompt를 제거했다. 이 endpoint는 404를 반환하고 OpenAPI에도 나타나지 않는다.
- 기본 QA agent와 planner–executer의 ORDERED·FREE context에서 승인된 시연 절차와 안내문을 제거했다. 예전 snapshot에 `verification_procedure`가 있어도 입력 schema가 무시하며 model context와 재저장한 snapshot에 남지 않는다.
- `run_config.prompt_hashes`에 시연 prompt를 추가하지 않는다. 기본 QA 구조는 `v35-case-evaluation-no-demonstrations`, planner 구조는 `pe5-language-no-demonstrations`로 구분한다. 도구와 실행 한도는 그대로다.
- 시연 원본과 storage 객체 삭제는 orchestration의 정리 migration·작업이 담당한다. agent는 Redis 사용 시 시작 단계에서 `qa:*` 세션의 시연 전용 필드와 prompt hash를 정리한다. 기존 QA 세션·일반 prompt hash·TTL은 보존하고 동시 수정이 있으면 다시 읽어 정리한다. 반복 충돌이나 저장소 오류가 있으면 시작을 실패시켜 재배포로 재시도한다. 이전 버전 agent를 먼저 중단해야 정리 후 과거 필드가 다시 기록되지 않는다.

## 검증

삭제된 endpoint·prompt, 과거 snapshot의 시연 필드 무시, 두 engine의 context·prompt hash, TC 보고·사람 피드백 보존을 자동 검사한다. 실제 Unity 게임을 대상으로 한 유료 QA 전후 비교는 실행하지 않았으며, 정확도나 성능 개선을 주장하지 않는다.

`docs/pr-consolidation-20261028.md`의 원본 PR 기록과 과거 arch 주석은 변경 이력이다. 해당 기록이 현재 시연 기능을 제공한다는 뜻은 아니다.

최종 검증에서 전체 pytest 2,959개와 focused 120개가 통과했다. 별도 Redis 7에서 Lua CAS 충돌·TTL 유지·재실행·없는 key 부활 방지·일반 QA 정보와 다른 namespace 보존을 검증했다. 로컬 검증 로그는 /tmp/artel-agent-removal-validation/에 보관했다.
