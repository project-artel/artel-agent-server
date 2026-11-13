# 2026-11-10 — 모델 입력 앞에 최신 Pulse 이력 추가

- Date: 2026-11-10
- Jira: None
- Status: Draft

## Goal

QA Agent가 매 호출에서 대상별 최근 값과 SDK가 보고한 제거를 볼 수 있게 한다.

## Non-goals

값 변화의 원인이나 조작과의 인과관계는 분류하지 않는다.

## Context / Constraints

Pulse 델타에 이름이 빠지면 마지막 값을 유지한다. `gone`, 비활성화, 전체 스냅샷에서 빠짐은 각각 표시한다.

## Approach (Checklist)

- [x] 기존 Pulse 상태와 제거 기록을 확인한다.
- [x] 최근 값 최대 5개를 보관하고 모델 호출 앞에 최신 요약을 붙인다.
- [x] 값 흐름, 제거, 재등장, 전체 스냅샷과 모델 호출 순서를 확인한다.
- [ ] 실제 QA run에서 판정과 입력 비용을 비교한다.

## Validation

- **Commands to run:** `python -m pytest tests/test_qa_latest_pulse_context.py tests/test_qa_arch.py`
- **Expected output:** 최신 요약 한 개가 대화 기록 앞에 들어가고, 제거된 대상의 값은 현재 상태에 나오지 않는다.

## Risks & Rollback

- **Risks:** 대화 앞에 동적 블록을 붙이면 프롬프트 캐시 재사용이 줄어들 수 있다.
- **Rollback steps:** 이 변경을 revert해 기존 도구 응답의 Pulse view로 복구한다.

## Open Questions

- 실제 QA에서 모델별 캐시와 판정 차이를 확인한다.
