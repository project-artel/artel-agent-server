---
version: v18
note: reset_game 도구 description 을 코드에서 옮김. 500자 이하.
placeholders: []
---
Reload the game's first scene for a clean state: the screen, score and inventory are gone, held input is released, every target id is dead. `clear_player_prefs=True` also wipes PlayerPrefs, irreversibly. Even with the flag on, the game's own save files are untouched and a step needing a fresh save still needs the operator. Full consequences: see the held_state skill.
