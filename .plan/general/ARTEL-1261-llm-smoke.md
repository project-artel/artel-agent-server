# 시연 추출 실제 LLM 검증 기록

2026-10-23에 OpenRouter의 `openai/gpt-6-luna`를 실제 호출했다. 기존 orchestration의 8091 내부 포트가 연결을 거절해 새 서버는 실행하지 않았다. 기존 로컬 자격증명을 출력 없이 `LlmKey` context에 바인딩했고, model catalog는 `tests/fixtures/model_catalog.json`을 사용했다. 실제 게임이나 HTTP endpoint 통합 검증은 아니다.

## Fixture와 결과

Pillow로 상점 전후 PNG 두 장을 만들었다. 구매 전에는 골드 100·포션 0, 구매 후에는 골드 90·포션 0이다. TC 기대 결과는 골드 90·포션 1이며 사람 판정은 FAIL이다. 입력 요청·SDK 결과·사람 설명을 함께 제공했다.

| 호출 | 추가 자료 | 소요 | 관찰 결과 |
| --- | --- | --- | --- |
| 1 | 화면·입력 결과 | 13.35초 | FAIL과 정상 기대 결과를 구분했고, 구매 완료 여부 등의 미해결 질문 2개를 남겼다. |
| 2 | SDK 구매 완료 `whole pulse` | 16.53초 | FAIL과 정상 기대 결과를 유지하고 미해결 질문은 없었다. facts에서 TC 42를 event 42로 잘못 인용한 문제를 발견했다. |
| 3 | 호출 2와 같은 자료, citation 보완 후 | 20.66초 | TC 42와 원본 event를 구별했다. FAIL과 정상 기대 결과를 유지하고 이미지·SDK 시점 차이에 대한 미해결 질문 1개를 남겼다. |
| 4 | TC 언어 우선 지시 보완 후 | 17.55초 | 한국어 절차를 생성하고 FAIL과 정상 기대 결과를 유지했다. |

세 호출 모두 sourceHash를 그대로 반환했고 단계와 assertion을 생성했다. 호출 2에서 발견한 문제를 반영해 prompt에 TC ID와 event ID의 차이를 명시하고, facts·inferences·preconditions의 `event N`/`이벤트 N` 인용이 실제 event에 속하는지 검사했다. 관련 테스트 91개가 통과했다.

이 단계의 초안에는 미해결 질문이 있어 그대로 승인할 수 없었다. 이후 사용자 피드백으로 TC에 시간 제한이 없으면 미측정 latency 자체를 승인 질문으로 만들지 않도록 보완했다. 실제 최소 입력 UI 시연과 ORDERED/FREE 결과는 구현 계획에 별도로 기록했다. 이 네 호출 결과를 판정 정확도나 QA 개선 효과로 일반화하지 않는다.

## 로컬 산출물

- `.venv/smoke_demonstration.py`: 자격증명을 기록하지 않는 재현 스크립트.
- `.venv/demonstration-smoke/`: 첫 호출의 request.json, response.json, report.json, before.png, after.png.
- `.venv/demonstration-smoke-complete/`: 마지막 호출의 동일 산출물. 두 번째 응답에서 발견한 인용 오류는 위 기록에 남겼다.

이 산출물은 `.venv` 아래의 gitignored 로컬 검증 자료다. 자격증명은 어느 산출물에도 저장하지 않았다.
