---
version: v46
note: 치트의 precondition 전용 제한을 제거하고 TC의 실제 동작과 결과를 확인하도록 안내. ARTEL-1340.
placeholders: []
---
Reload the game's first scene for a clean state: the screen, score and inventory are gone, held input is released, every target id is dead. `clear_player_prefs=True` also wipes PlayerPrefs, irreversibly. Even with the flag on, the game's own save files are untouched and a step needing a fresh save still needs the operator. Full consequences: see the held_state skill.
