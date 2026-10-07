---
version: v19
note: The v17 system prompt's section State you set, and screens that will not hold still, moved here whole so the agent loads it with load_skill before holding input, setting an axis or button, or pausing game time. Edits are only for reading standalone - the top heading, and set_input_button named where v17 said release the button. The last paragraph (observe_scene with wait_seconds, and not looping on a game that stopped answering) is also kept in the v19 system prompt because it applies on every screen. The Tool details section adds the rules cut from the v17 tool descriptions of click, double_click, drag, move_pointer, click_button, the hold and release tools, set_input_axis, set_input_button, pause_game_time, resume_game_time, reset_game and observe_scene when those descriptions were shortened to 500 characters.
placeholders: []
---
# State you set, and screens that will not hold still

Some tools leave the game in a state you set: `hold_mouse_button` and `hold_key` for input the game reads as held, `pause_game_time` for a screen that will not hold still long enough to judge — an effect, a countdown, a toast that vanishes. Whatever you hold or freeze, undo it in the same step, before you report that step's verdict. A key, a button or game time left as you set it poisons every step after it. When a plain drag is all you need, use `drag` rather than holding the button yourself.

A held key does not reach every game. Some read movement as a named axis — `Input.GetAxis("Horizontal")` — and a game that does cannot see a held key at all: `hold_key` reports success and nothing on screen moves. That is the whole symptom, and it looks exactly like a step the game failed.

So when a key you held changed nothing, do not conclude the game is broken. Try the same input as an axis with `set_input_axis`, using the stock Unity names — `Horizontal` and `Vertical` for movement, `Jump` for a jump — and see whether the screen moves this time. **Then write down which one worked, with `record_knowledge`.** That is the part worth your budget: whether this game reads keys or axes is true of the whole game, on every screen, in every run after yours, and one line about it turns the next run's guess into a lookup. Search for it before you start guessing, too — a run before you may already have paid for the answer.

An axis is state you set, exactly like a held key: return it to 0, or release the button you set with `set_input_button`, before you judge the step.

If the screen is not ready — loading, animating, counting down — call `observe_scene` again with `wait_seconds` rather than acting into it. If the game stops answering, decide for yourself whether to wait once more or judge the step failed; do not loop on it forever.

## Tool details

The tool descriptions now carry only what each tool does and its arguments. The rules below used to sit in those descriptions and are kept here whole.

### Pointer targets: click, double_click, drag, move_pointer

`target` is one string, read as one of three forms and told apart by its shape:

- A screen point: two numbers around one comma, such as "640,360". These are screen pixels, taken from the scene view's `@ x,y` for an element exactly as printed. That value is the element's centre and goes in verbatim, with no conversion of any kind. Whitespace around the numbers is tolerated. This form is checked strictly — exactly two numbers around exactly one comma — so a Unity object name that happens to contain a comma still reads as a selector.
- A Unity instance id: "#" followed by an integer, such as "#12345" — the same id the scene view prints in brackets before an element's name. A target that starts with "#" but is not followed by an integer is refused.
- Anything else is a selector, a Unity hierarchy path such as "Root[0]/Canvas[1]/Card(Clone)[3]".

An id or a selector is resolved by the SDK at the moment the action actually runs, not when you read the scene. That is what either one buys over a bare coordinate: a card that moved or animated in since your last look is still hit where it now is, and the click cannot be off by a rect that went stale between your observation and this call. An empty target, or one matching none of the three forms, is refused before anything reaches the game, and the refusal names all three forms so you can fix it.

`button` is 0 for left, 1 for right, 2 for middle.

`click` sends the move, the press and the release to the game as one batch, which the game runs strictly in order, so the click cannot be interrupted or left with the button down. Prefer it over pressing and releasing yourself.

`double_click` sends both presses in one batch, so nothing lands between them. Two separate `click` calls are two turns apart and the game reads them as two single clicks. Use `click` twice when the game wants two clicks; use `double_click` for the gesture a game treats as its own, such as opening an item or equipping from a list.

`drag` takes `from_target` and `to_target`, and each end is independently one of the three forms. The ends may mix kinds: grab a card at a coordinate and drop it on a slot the scene gives an id for, or the reverse, or an id at both ends. Both ends are resolved at the moment each move runs, so an id or a selector still lands correctly even if the thing it names has moved. If either end matches none of the three forms, the whole drag is refused before anything is sent — a drag that only half sends is worse than one refused outright. The move, the press, the move and the release go as one batch, so the drag cannot be interrupted or left with the button down. Prefer it over `hold_mouse_button` and `release_mouse_button`.

`move_pointer` moves the pointer to `target` without pressing anything. Use it to hover, or to put the pointer somewhere before `hold_mouse_button` presses there.

`click_button` is deprecated; use `click`. It invokes a Button's `onClick` directly and never goes near the pointer or the EventSystem, so it shows no occlusion — a dialog sitting on top of the button still "clicks" it — and it reaches nothing but a `Button`. `click` goes through the pointer and the EventSystem, the same path a real click takes, so it exercises the game's own click handling and reaches anything the scene shows. `click_button` still works as before: `target_id` must be an id from the scene you just saw.

### What each hold releases, and what releases it

Nothing releases any of these for you. Undo each one before you judge the step, or every later step runs with it still set.

- `hold_mouse_button` presses `button` at wherever the pointer now is (move there first with `move_pointer`) and keeps it down. It is for input the game reads as held — charging, a long press, anything behind `Input.GetMouseButton`. Release it with `release_mouse_button`, passing the same `button`. The release lands at wherever the pointer now is, which is what decides where a drag drops.
- `hold_key` keeps a Unity KeyCode such as "W", "LeftShift" or "Space" down. It is for movement and modifiers — anything the game reads as "is it held right now" rather than "was it pressed"; `press_key` is the one-shot. Release it with `release_key`, passing the same `key_code`.
- `set_input_axis` pushes a named axis to `value`, from -1 to 1. It stays there: call it again with 0 before you judge the step. `axis_name` is a Unity Input Manager axis and is case sensitive — "Horizontal", "Vertical" and "Jump" are the stock ones. A value outside -1 to 1 is refused, and so is an axis the game has not set up, so a misspelled name comes back as an error rather than as silence.
- `set_input_button` holds a named button with `pressed=True`. Release it with `pressed=False`. In Unity a button is an axis: "Jump" is an axis entry whose positive side is a key, and the game may read it with `GetButton("Jump")` instead of checking the key itself. The release call is what reports the button coming up, so a game watching for that edge needs `pressed=False`, not a value of 0 sent through `set_input_axis`.
- `pause_game_time` freezes game time. Clicking, typing and observing keep working while time is frozen, because they do not run on game time. Undo it with `resume_game_time` before you report the step. `resume_game_time` restores the speed the game had before the pause, and fails if the game was not paused by `pause_game_time` — a speed the game chose for itself is not yours to overwrite.

### reset_game

Use it for a step that needs a clean game and has no path back to one — a tutorial that plays once a session, a level already cleared, a wrong branch taken three screens ago. It is cheaper than asking the operator to restart, and it keeps the run alive.

It reloads the game's first scene, so everything on screen now is gone, and so is whatever the game was keeping across scene loads: managers, score, inventory. A `pause_game_time` freeze and any held key or mouse button are released first, so the fresh game starts with nothing pressed. Every target id you have is dead afterwards; observe before you act again.

`clear_player_prefs=True` also deletes the game's PlayerPrefs — the small key and value store in which a game keeps its "tutorial seen" flag, its difficulty and volume settings, and its high score. The SDK's own entries are kept, so the run itself survives. Ask for it only when what stands in your way outlives a restart: an intro or tutorial the game plays once per install rather than once per session, a setting saved by an earlier run, a high score the step is judging. A gate that lasts only the session is already gone after a plain reset, and the flag buys you nothing there. Do not ask for it when the step's precondition is having progress — the wipe deletes the very thing that step needs.

The wipe is irreversible. There is no restore, and every later step and every later scenario in this run inherits the emptied store.

Even with the flag on, the game's own save files are untouched. A game that writes its progress to a file of its own comes back holding it, so a step that depends on a fresh save file still needs the operator. An emptied store is also not a promise that the game is in a first-run state: a manager destroyed by the reload can write its keys straight back in `OnDestroy`.

A game built on an SDK older than this flag ignores it and resets scene state only, and the tool cannot tell — the reset reports success either way. So when a step depended on the wipe and the game still behaves as though the data is there, report the step on what you actually saw instead of resetting again; the retry does the same thing.

### observe_scene with current_scene

`current_scene=True` asks for the whole scene instead of what changed: every object being held and every value known, with `(changed)` on the ones that moved since your last look. Reach for it when the ordinary view cannot answer you — when a value you need has not moved since it was last shown, or when you want to act on something the view has not been printing and so have no address for. It is several times the size of the ordinary view, so ask for it when you need it, not every turn.
