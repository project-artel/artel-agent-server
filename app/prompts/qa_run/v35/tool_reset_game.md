---
version: v35
note: TC 단위 판정은 v30 본문을 유지하고 중간 QA snapshot과 판본을 구분.
placeholders: []
---
Reload the game's first scene for a clean state: the screen, score and inventory are gone, held input is released, every target id is dead. `clear_player_prefs=True` also wipes PlayerPrefs, irreversibly. Even with the flag on, the game's own save files are untouched and a step needing a fresh save still needs the operator. Full consequences: see the held_state skill.
