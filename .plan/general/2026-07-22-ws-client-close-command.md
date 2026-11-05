# 2026-07-22 - WS 클라이언트 종료 명령(`type: "close"`) 추가

- Date: 2026-07-22
- Jira: None
- Status: Done

## Goal

세션 WebSocket에서 클라이언트가 `{"type":"close"}` 메시지 하나로 세션을 즉시 종료할 수 있게 한다. 종료 신호가 소켓을 쥔 바로 그 연결로 들어오므로 소켓 레지스트리나 Redis pub/sub 없이 핸들러가 자기 소켓을 스스로 닫는다.

## Non-goals

- 승인/거부(approve/decline) 의미 구분은 두지 않는다. 해당 판단은 orchestration 계층에서 처리하고, agent 서버는 세션 정리와 소켓 종료만 담당한다.
- WS 밖(제3자)에서 종료를 밀어넣는 경로(Redis pub/sub, keyspace notification)는 이번 범위에서 제외한다.
- 기존 HTTP `approve`/`decline` 엔드포인트 정리는 별도 결정으로 미룬다(둘 다 `service.close()` 호출로 동일).

## Context / Constraints

- 기존 WS 핸들러는 `receive_json()`에서 다음 turn만 기다리며, `approve`/`decline` HTTP 호출은 Redis 세션만 삭제할 뿐 열려 있는 소켓을 닫지 않아 lazy 종료(다음 turn 전송 시점에야 `session_expired`)였다.
- 종료 요청 주체 = WS 클라이언트(orchestration)라는 전제이므로, WS 인바운드 메시지로 종료를 처리하는 것이 가장 단순하고 멀티프로세스에서도 안전하다.
- 첫 연결 시 클라이언트는 아무것도 보내지 않아도 된다. `POST /sessions`의 `user_input`이 `pending_user_input`으로 저장되어 연결 즉시 첫 `result`로 자동 푸시된다.

## Approach (Checklist)
- [x] **Step 0: Recon** (`app/api/sessions.py` WS 루프, `SessionService.close`, `RedisSessionStore` 확인)
- [x] **Step 1: Implementation** (`app/api/sessions.py`: WS 루프에 `type == "close"` 분기 추가 → `service.close()` → `{"type":"closed"}` 응답 → `websocket.close()`)
- [x] **Step 2: Tests** (`tests/test_sessions.py`: `test_ws_close_terminates_and_deletes_session` 추가 — close 후 재연결 시 `session_expired` 검증)
- [x] **Step 3: Rollout / Rollback** (스키마 추가 없음, 하위 호환 — 기존 `turn` 흐름 그대로)

## WS 계약(변경 후)

| 방향 | 메시지 |
|---|---|
| 연결 직후(자동) | `{"type":"result", ...}` |
| 클라이언트 → 서버 | `{"type":"turn", "user_input":..., "model":...}` |
| 클라이언트 → 서버 | `{"type":"close"}` (종료) |
| 서버 → 클라이언트 | `{"type":"result"}` / `{"type":"error"}` / `{"type":"closed"}` |

## Validation
- **Commands to run:** `python -m pytest`
- **Expected output:** All tests pass (신규 `test_ws_close_terminates_and_deletes_session` 포함).

## Risks & Rollback
- **Risks:** WS를 쥐지 않은 제3자가 종료해야 하는 흐름이 생기면 이 방식으로는 소켓을 닫을 수 없다(그 경우 Redis 신호 기반 필요).
- **Rollback steps:** `app/api/sessions.py`의 `close` 분기와 신규 테스트를 되돌린다.

## Open Questions
- HTTP `approve`/`decline` 엔드포인트를 유지할지, `close` 하나로 합칠지 — orchestration이 agent 서버를 WS로 부르는지 HTTP로 부르는지에 따라 결정.
