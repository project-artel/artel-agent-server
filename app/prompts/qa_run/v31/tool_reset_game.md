---
version: v31
note: step 보고 계약은 v24를 유지하고 이 단계의 QA 기능만 적용.
placeholders: []
---
Reload the game's first scene for a clean state: the screen, score and inventory are gone, held input is released, every target id is dead. `clear_player_prefs=True` also wipes PlayerPrefs, irreversibly. Even with the flag on, the game's own save files are untouched and a step needing a fresh save still needs the operator. Full consequences: see the held_state skill.
