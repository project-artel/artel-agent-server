# TC 단위 판정과 질문 대기 검증 기록

검증일: 2026-10-27 (Asia/Seoul)

## 변경 범위

`report_step`·`report_case`를 QA tool 목록에서 제거하고 `start_test_case`·`report_test_case`로 검증 시도를 기록한다. step은 게임 조작과 timeline의 참고 위치로만 사용한다. 미검증 TC와 판단 불가 결과를 실패로 바꾸지 않는다.

QA 구조는 `v28-case-evaluation`, 기본 prompt는 `qa_run/v26`이다. 기존 prompt 버전은 보존한다. HTTP 응답 유실 시 같은 시도 ID와 최초 보고 내용을 재전송하고, 반복 검증은 새 시도로 남긴다. 사용자 질문 대기는 실행 제한 시간에서 제외하며 다른 tool 실행도 답변까지 대기한다.

## 자동 검증 결과

WSL Ubuntu의 Linux 임시 디렉터리에 동일 소스를 복사해 `python -m pytest -q`를 실행했다.

| 검증 | 결과 |
| --- | --- |
| 전체 pytest | 2,889 passed, 234.88초 |
| context·prompt 집중 검증 | 179 passed |
| TC·knowledge·behavior 집중 검증 | 149 passed |
| 미사용 v26 prompt 삭제·EOF 정리 후 lock·version·arch 검증 | 85 passed, 36.49초 |
| `git diff --check` | 통과 |

전체 테스트에는 step 완료 조건 제거, 반복 TC 원본 보존, 미검증·판단 불가 집계, 질문 대기 중 다른 tool 차단, 대기 시간 제외, 실행 취소, 최초 payload 재시도, 시도별 macro 초안 분리와 knowledge 인용 거절 수 전달을 포함한다.

마지막 prompt 정리 후에는 `python -m app.prompts.lock --write`로 lock을 재생성하고 `python -m pytest -q tests/test_prompts_lock.py tests/test_qa_prompt_version.py tests/test_qa_arch.py`를 실행했다. 이미 배포된 이전 prompt 버전은 수정하지 않았다.

## 실제 게임 비교 미실행

실제 Unity 게임을 같은 model·prompt_version으로 변경 전·후 실행하는 비교는 완료하지 않았다. 로컬 검증 DB의 LLM 키를 복호화할 서버 설정이 없어 사용자에게 설정 파일 또는 검증 환경 경로를 요청한 상태다. 비밀 값 자체를 문서에 넣지 않는다.

자동 테스트 통과를 agent 정확도 개선의 측정 결과로 해석하지 않는다. 합성 영상으로 확인한 orch·S3 통합 검증도 실제 게임 QA 성능 비교를 대신하지 않는다. 배포 전에 같은 scenario·build의 변경 전·후 `run_config`와 TC 결과를 함께 기록해야 한다.

## 연관 문서

- [확정 요구사항](qa-tc-evaluation-requirements.md)
- Jira: ARTEL-1270, 전체 작업 ARTEL-1268
