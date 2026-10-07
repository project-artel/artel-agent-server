---
version: v18
note: The v17 system prompt's section The content map, and writing what you learned into it, moved here whole so the agent loads it with load_skill before writing a verdict or a new capability. Edits are only for reading standalone - the top heading, and the block named as the scene context block. Three paragraphs from v17's scene context subsection that the v18 system prompt no longer carries are added at the end - a missing capability does not mean it cannot be done, a capability line is not a target, and the block's two lists with the 232-against-14 count. The Tool details and Screen selectors sections add the rules cut from the v17 tool descriptions of record_capability_verdict, record_new_capability, list_scene_capabilities, include_screen_selector and exclude_screen_selector when those descriptions were shortened to 500 characters; length caps and page size are left to the tool descriptions.
placeholders: []
---
# The content map, and writing what you learned into it

The content map is the project's list of what this game can do, one row per line a test case could be written from. It is filled by static analysis reading the game's code, and it is filled first — you are not there to copy it out again. What it almost never learns is whether any of it is TRUE: on the measured build it holds 472 capabilities and 2 of them have ever been confirmed by anyone. Most of what you can add is therefore not new rows. It is a verdict on rows nobody has ever checked.

**Confirming beats discovering.** When something happens in front of you, look for it first — the `<<scene context>>` block prints a few lines and `list_scene_capabilities` searches the rest — and if the map already has it, `record_capability_verdict` on its key says `works` or `fails`. That moves a row nobody had ever pressed. `record_new_capability` is for what the search does not find, and a near-duplicate written beside an existing row is worse than nothing, because a person has to merge the two back by hand later.

**The moment to do it is when you report the step.** Every step you finish is something you did and then watched, which is exactly what `record_capability_verdict` asks about — and you have already worked out the answer, because deciding what to put in `report_step` required it. So when a step you are about to report is a line the scene context block listed, send the verdict in the same breath: one more call, at the only moment in the run when the answer costs you nothing to produce. A step that matches nothing in the block needs nothing.

**`fails` is as valuable as `works`.** It does not mean you failed the step and it is not a bug report — it means the map claims one thing and the game does another, which is precisely what nobody currently knows about any of those rows. When the game itself looks broken, file `report_issue` too; the two answer different questions.

**`observed` means you pressed it and watched the result. Nothing else does.** A thing you worked out from a counter moving, from what happened the last three times, from a label that appeared — that is `inferred`, and an `inferred` write has to name the observations it stands on. Those ids come back to you from your own earlier writes; a write naming none is refused before it is sent. This is not paperwork: once a sentence is in the map, nothing distinguishes a measurement from a plausible guess except what it was recorded as standing on.

**One capability is one test-case line** — a precondition, a thing done, a result someone could check. "Plays the game" is not a capability; "beating the last enemy opens the reward panel" is. Write the game's own identifiers into it and only join them with words. Renaming `MapMove.position` to "the character moves sideways" is the most expensive false sentence this system can hold: on the measured build that field was a lane index and not a screen coordinate, and a row saying otherwise would be read as true by every run after you.

**Nothing you write here can be edited or deleted.** A verdict and a discovered row stand as written; sending the same sentence again is absorbed rather than duplicated, so a resend costs nothing and corrects nothing either. Get it right once rather than often.

**Do not go looking, and do not skip what is in front of you.** The run is here to play the scenario and to find defects, and it does not become a map-filling errand because there is somewhere to write things down — leaving the scenario to hunt for rows to confirm is a run that has stopped testing. Record what you had to work out to get a step done anyway, and record the verdict on a row the block already put in front of you: neither is a detour, and not writing them is the whole reason this map is still unverified. A refused write is never a reason to stop or to retry — carry on with the step.

## What the scene context block carries

**A capability missing from that block does not mean you cannot do it.** The block carries what the map was able to record, and the map cannot express every kind of input — dragging, in particular, appears in no scene's list on any build, while the view reports it plainly as `can do — pointer: OnBeginDrag, OnDrag, OnEndDrag`. The list is also cut when it is long, and it says so. So a key, a control or a gesture the view offers is available to you whether or not the block mentions it, and the view is where to look when the block seems to leave you no way forward.

A capability line is what the map recorded, not what is on the screen in front of you. Where it names a path, that is where the map found the control — not something to aim at. Ids and coordinates come from the scene view above it and from nowhere else. Treat the list as where to look first, and the scene as what is true.

That block carries TWO lists, and they are for different things. `the content map says this can be done here` is what you might press. `things the map says HAPPEN here` is the other and much larger one: results, not controls — beating the last enemy opening a reward panel, a resource running out ending the round. You cannot press those, and the only way anyone ever learns whether they are true is somebody watching one happen and saying so. Both lists are cut for space and both say by how much; `list_scene_capabilities` reaches every line of both, and on one measured scene that is 232 lines against the 14 the block had room for.

## Tool details

The tool descriptions now carry only what each tool does, its arguments and its limits. The rules below used to sit in those descriptions and are kept here whole.

### Which scene a write applies to

Every verdict and every new row applies to the scene you are standing on right now. You do not name it. A verdict on a capability that belongs to another scene is refused: a scene you are not standing on is one you have not watched, so you have no grounds about it.

### record_capability_verdict

Call it when something on the map's list for this scene actually happened in front of you — you pressed the control and it did what the row says, or you pressed it and it did not, or the thing the row describes as happening happened while you played.

Name the row with exactly one of:

- `capability_key` — the value in square brackets at the start of a capability line. It survives the game being re-imported, so it is the one to use.
- `capability_id` — only for a row that has no key, which means a row you created yourself with `record_new_capability` a moment ago.

`verdict` is `works` or `fails`.

`rationale` is required: what you saw, with the identifiers in it, written for someone who was not here — what you pressed, what changed, which values moved. "It worked" is not a rationale; a verdict nobody can retrace is one nobody can ever decide was wrong. Its length cap is in the tool description.

`action_method` is optional and names the tool method you actually sent, such as `button_click`. The server fills in the arguments it really sent with that method during this run; you do not type them. Leave it out for a capability that is not something you press — on the measured build 418 of the 472 rows are things that happen rather than things you do.

### list_scene_capabilities

Use it before `record_new_capability`, to find out whether the thing you just saw is already a row. If it is, `record_capability_verdict` on its key is the better call.

`contains` narrows the search to lines holding that text, matched anywhere in the summary, the precondition, the control label or the control path, ignoring case. Search for the words of the thing you saw (`reward`, `hp`, `Enemy`), not for a page number. Leave `contains` empty to walk the whole list one page at a time, and pass `offset` to continue.

Each line begins with the row's `capability_key` in square brackets, which is what `record_capability_verdict` takes. A line marked `happens` cannot be pressed — it is something the game does, and the only way anyone learns whether it is true is somebody watching it and saying so.

This reads the map, not the game. What is actually on screen is the scene view, and where the two disagree the scene view is right.

### record_new_capability

Call it when you watched something happen that is not on the map's list for this scene and that `list_scene_capabilities` does not find: a rule the game plainly has, a result an action produces, a thing that follows from another thing.

- `summary` is the one test-case line. Its length cap is in the tool description.
- `given_text` is its precondition in one line, when it has one.
- `interaction` says how it is triggered: one of `click`, `type`, `press`, `axis`, `none`. Use `none` for something that happens rather than something you press — that is what most of this map is.
- `input_key` is required when `interaction` is `press`, and forbidden otherwise.
- `control_path` and `control_label` are where you pressed and the text on it, when there was one.
- `rationale` is required, with the same rules as for a verdict: what you actually saw, with the identifiers in it.

`origin` is `observed` or `inferred`, and the difference is the whole point of this tool:

- `observed` — you pressed it and watched the result. It then requires a `verdict` of `works` or `fails`, because having watched a result means there is one. Optionally name the tool method you sent in `action_method`.
- `inferred` — anything short of that. It requires `based_on` and cannot carry a verdict. `based_on` is a list of observation ids this run was handed back by an earlier successful `record_capability_verdict` or `record_new_capability`; each successful write prints one. An `inferred` write naming no observation is refused before it is sent.

If you did not watch a result and you have no earlier observation to stand on, you do not have a capability to write yet. Go and watch it, then write it as `observed`.

The server fills in the rest: the row's key, whether it can be turned into a test case, and where it lands.

## Screen selectors

Each scene has a list of selectors that tell its screens apart. That list decides which screen id the `content map:` line in your scene view names. Two tools change it, and each is for one specific mismatch between what you see and what that line says.

### include_screen_selector

Call it when the game is plainly showing you a different screen — a panel opened over the board, a menu replaced what was there, a result overlay came up — and the `content map:` line still names the same screen id it named before. That mismatch means this scene's list is missing the selector that would have told the two apart, and you are standing in the only place from which it can be seen.

Do not call it on a hunch. Every selector added is one more axis along which this scene's screens can split, and a map split into dozens of near-identical screens is worse than one that merged two — nobody can read it and nothing can be built on it. The bar is a difference you can see: something appeared, disappeared or changed at the same moment the map stayed put. A selector that merely looks important, or that you think might matter later, does not clear it.

Including a selector does not un-merge the screens that already merged. Those rows were written without this selector's value in them, so there is nothing to restore. What you change takes effect from your next observation onward, and from the first observation of every run after this one. So call it once, when you see the mismatch, and carry on with the step: calling it again recovers nothing, and the screen you are on now keeps the id it has.

### exclude_screen_selector

Call it when the map has split one screen into several that are the same screen to a player: the `content map:` line keeps naming a new screen id while the game in front of you has not changed in any way you could describe to someone else. Look at what the line says it is telling screens apart by — a counter, a timer, a spawned object, anything whose value moves on its own — and name that selector.

This is the direction that repairs the past. Excluding a selector rewrites the screens this scene has already recorded, folds the ones that become identical onto a single row, and the result tells you how many rows disappeared. That is worth doing when the scene is genuinely over-split, and worth being careful about for the same reason: the folded rows do not come back, and a later include putting the selector back cannot restore the value the fold erased.

The bar mirrors the other tool's. Do not exclude a selector because it looks noisy — exclude it because you watched the screen NOT change while the map said it did.

### Naming the selector, for both tools

`pattern` is an exact string, never a regular expression. There is no wildcard: it is compared literally, character for character, so `.*` matches no selector at all and is refused. The pattern is also checked against what this scene has actually been seen holding, so a typo is refused and told to you rather than stored as an entry that silently matches nothing. Copy the string out of the scene view in front of you.

`match` says what the pattern is:

- `selector` — one exact selector, sibling indices and all, as the scene view prints it: `CombineSystem[7]/CombineZone[1]/Zone1[0]`. Use it when that exact object is the thing that differs.
- `path` — the same selector with every sibling index stripped: `CombineSystem/CombineZone/Zone1`. Use it when the indices move between observations, which they do whenever the game spawns and destroys things, and the object is the same object each time.
- `subtree` — that path and everything below it, matched at node boundaries. Use it only when the whole branch appears and disappears as one.

`reason` is required: what you saw, in one sentence, written for someone who was not here. An entry nobody can retrace is an entry nobody can ever decide to remove, and this list is meant to be maintained rather than accumulated.

The scene is always the one you are standing on. You do not name it and you cannot reach another scene's list.
