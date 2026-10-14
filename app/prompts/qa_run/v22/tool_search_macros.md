---
version: v22
note: search_macros 도구 description (ARTEL-984). 500자 이하. 첫 메시지 목록은 12개에서 잘리므로, 거기 없는 macro 를 새로 쓰기 전에 찾게 한다.
placeholders: []
---
Search the macros registered on this build by name and docstring. `query` says what the macro should do, in your own words; `limit` is 1 to 10, default 5. Each result is the macro's `def` line, the scenes it is used on, and its docstring. Use it before writing a new macro when the list in your first message has nothing that fits — that list is cut at 12. Nothing reaches the game. `read_macro` shows a result's source; `run_macro` calls it.
