---
version: v44
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Press a key once, with no target, so it works on a screen with nothing clickable. `key_code` is a Unity KeyCode name such as "Space", "Return", "Escape". `duration_seconds` must be greater than zero. It does not keep the key down; for that use `hold_key`.
