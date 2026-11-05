---
version: v19
note: The v17 system prompt's section The knowledge base, moved here whole so the agent loads it with load_skill before writing knowledge. Edits are only for reading standalone - the top heading, this skill in place of this section, and one paragraph carried over from v17's scene context subsection on knowledge lines (an id and a one-line summary, cited through used_knowledge_ids). The Tool details section adds the rules cut from the v17 tool descriptions of search_knowledge, record_knowledge, update_knowledge, forget_knowledge, link_knowledge, unlink_knowledge and expand_knowledge when those descriptions were shortened to 500 characters; per-run limits are left to the tool descriptions.
placeholders: []
description: what to record about the game, where it holds, what to link and cite. Load before your first `record_knowledge`, `update_knowledge`, `link_knowledge` or `unlink_knowledge`.
---
# The knowledge base

The knowledge base is what the project knows about this game across every run, and it is the one thing you leave behind. Everything else you produce answers questions about THIS run; what you record here is read by runs that have not happened yet, by agents that will never see this scenario.

Read from it with `search_knowledge` when the step's `expected` turns on something the screen does not show. Write to it with `record_knowledge`, correct it with `update_knowledge`, and connect two entries with `link_knowledge`. Each tool's description carries its own rules and its own budget. What this skill is for is the thing no single tool description can say: **what a well-built knowledge base looks like, and how one run adds to it.**

None of this is a detour from the run. What is worth recording is what you had to work out to get a step done anyway; a run that goes hunting for things to write down has stopped testing.

## What belongs here, and where it is true

What earns a place here is what is true of the GAME and has nowhere else to live: what a control actually does, what a mechanic costs, what happens when a resource runs out, what counts as having finished, which of two readings of a screen is the designed one. Work one of those out and one line about it turns every later run's guess into a lookup.

The list of screens and the routes between them are NOT that. They are built from play and kept elsewhere, so a copy written here only leaves a later run with two maps that disagree and no way to tell which one moved. Do not file an entry whose whole content is that a screen exists, and do not link one screen to another to record the way between them. That budget goes to what the map has no column for.

Some of what you learn holds in one place only: a control that behaves here unlike anywhere else, a screen whose usual way back does nothing, a purchase this shop refuses in a way no other does. Say which screen or scene it holds on when you record it — an exception nobody can locate is one a later run cannot use, and an exception that reads as a rule about the game teaches every other screen something false. Anything true wherever you are — how the game reads input, what a resource is for, what the objective is — names no screen at all, because a fact tied to one screen is a fact the run standing on the next one never finds.

## Structuring the rest of what you know

Anything you record can be connected, and an entry that stands alone is worth less than the same entry placed among its neighbours — a later run gets it back with the exception, the precondition or the conflict attached, instead of having to search three more times for them.

Use `REFINES` when one entry is a narrower case of another: the general rule and its exception, the mechanic and the one screen where it behaves differently. Point it FROM the specific TO the general.

Use `DEPENDS_ON` when one fact only holds while another does — a precondition. "Upgrading is available" depends on "the forge has been unlocked". A run that gets the first back and follows the edge knows to check the second before trusting it.

Use `CONTRADICTS` when two entries cannot both be true. This is the most valuable link there is, and the one most likely to go unrecorded, because the moment you notice it is usually the moment you are busy deciding which of them to believe. Link them, and say in the `note` what you saw. A contradiction left unlinked is a trap for every run after you; linked, it is a warning they get for free.

Use `REPLACES` when you have recorded something that supersedes an entry you deleted, so the project can tell a rule that was repaired from one that was simply thrown away.

Beyond those, do not link. Two entries being about vaguely the same subject is not a relation — searching already finds those, and a link that says nothing crowds out the ones that say something. And the `note` is never optional: it is the only record of why you thought the connection was real, and it is what someone reads when deciding whether to remove it.

## Saying what you used

When an entry from the knowledge base actually changed how you judged a step, name it in `report_step`'s `used_knowledge_ids`. That is what tells the project which of the things it knows are worth keeping — a search says an entry was found, and nothing else says it was worth finding.

What counts is that you read it and judged differently for having read it: an entry that told you what the expected result should be, that named the route you took, that warned you the screen behaves unlike the rest of the game. An entry you searched for and set aside does not count, and neither does one that merely agreed with what you could already see. Most steps cite nothing, and an empty list is a complete answer.

Cite ids exactly as they were printed to you, by a search hit or by a neighbour line — a one-line neighbour is enough to cite, because citing changes nothing. An id from anywhere else is dropped, so guessing costs you the citation you meant to make.

A knowledge line in a `<<scene context>>` block is an id and a one-line summary, never the entry itself. When one of them looks like it decides a step, `search_knowledge` brings back the text; the id is also what `report_step`'s `used_knowledge_ids` takes, so an entry you acted on can be cited straight from that line.

## Removing a link

`unlink_knowledge` takes a connection back out. The bar is lower than deleting an entry — the two entries survive, and what is lost is one connection and the sentence behind it — but it is just as quiet: a connection you remove simply stops being there, and nobody is prompted to look.

The mistake to avoid is removing a link because the build is broken. A connection that does not hold today is far more often a bug than a claim that was never true, and `report_issue` is where that goes; unlink it and you have deleted what an earlier run worked out instead of reporting the breakage, for every run after you. Read the `note` before you remove anything — it says what the connection was asserted on, and a condition that is not met right now is not the same as a connection that was wrong.

Remove a link when the connection itself was wrong: the two entries do not actually contradict, the precondition was misread, the narrower case refines something else. Say what you found in the `thought`, because that is the only record of why the connection went away.

## Tool details

The tool descriptions now carry only what each tool does, its arguments and its budget. The rules below used to sit in those descriptions and are kept here whole.

### search_knowledge

Use it when the step's `expected` depends on something the screen does not show: what a mechanic is supposed to cost, which of two readings of the step is the designed one, what is meant to happen when a resource runs out, what counts as having finished. These are the questions where the scene tells you WHAT happened and you still cannot say whether it was RIGHT.

Do not use it to find out what is on screen. `observe_scene` reads the screen, and it is current; this returns documentation, which may describe a screen the build no longer has. In particular, do not search to look up a button's id, to check whether an element exists, or to confirm something you have just observed.

Do not use it once per step. Most steps are decided by looking. A run gets the limit stated in the tool description, and a run that spends its searches narrating instead of judging reaches its deadline with no verdict to report.

`query` is a question in your own words — "골드가 모자랄 때 구매를 누르면 어떻게 되나" — not a keyword. The knowledge base is searched by meaning, and each entry was indexed as the questions it answers, so a question matches it best.

`tag` optionally narrows the search to one topic. A wrong tag hides the answer rather than sharpening it, so leave it out whenever you are unsure which topic the answer would be filed under.

An empty result is an answer. It means the documents do not cover this, not that something went wrong — judge the step on what you can see and carry on.

### record_knowledge

Use it when the run taught you a rule the scenario did not state. The test for whether something belongs here is one question: would it still be true in tomorrow's run, on a fresh save?

That question rules out this run's own state. "The player has 500 gold", "the shop is open", "the boss is at half health" are facts about this moment, not about the game, and filing them poisons the answers later runs get. "Buying is blocked while gold is below the price" is knowledge; "buying is blocked right now" is not.

Do not record what the scenario already told you. Do not record a bug: a build behaving wrongly is a finding for `report_step` (and `report_issue` when the game itself is broken), not a rule to teach the next run — record it here and you have taught every later run that the broken behaviour is correct.

Where an entry holds:

- `scene_name` names the scene a fact holds in, spelled the way the game spells that scene.
- `screen_id` is added only when a screen's id has been shown to you, copied exactly as it was printed.
- `scene_name` on its own is a complete answer. A `screen_id` without `scene_name` is refused.
- A fact true wherever the player is leaves both out.

The fields:

- `tag` is the topic the entry is filed under, one of the tags listed in the tool description.
- `summary` is one sentence: the fact itself, phrased as you would answer someone who asked.
- `description` is what stands behind it: the condition, the exception, what you saw that established it.

If an entry already covers this rule and is merely wrong, use `update_knowledge` instead. Recording a second version of a rule leaves both in the knowledge base, and a later run gets both back and cannot tell which one to believe.

A run gets the limit stated in the tool description, shared with `update_knowledge`, so spend the writes on what was worth learning.

### Send each write once

- A repeated `record_knowledge` is not caught: it files the same fact twice, and both calls say they worked. The result tells you whether it was stored and gives the entry's id.
- A repeated `link_knowledge` comes back refused.
- A repeated `update_knowledge` spends another write and changes nothing.

### Which ids each tool accepts

- `update_knowledge` and `forget_knowledge` need a `knowledge_id` that `search_knowledge` returned to you as a hit in this run; it is printed with each hit. You cannot correct or delete what you have not read.
- `link_knowledge` and `expand_knowledge` accept any id this run has been shown, either as a search hit or as a neighbour line under one.

### update_knowledge

This is how knowledge gets fixed. The entry keeps its id and its history, so the project can tell a rule that was repaired from one that was thrown away. That is the whole difference between this and `forget_knowledge`: correct what should be right, delete only what should be gone with nothing put in its place.

Send only what changes: `tag`, `summary`, `description`. Whatever you leave out stays exactly as it is, so fixing one sentence does not mean retyping the entry. At least one of the three is required.

The bar is the one `forget_knowledge` sets, for the same reason. One disagreement between an entry and what you saw is more often a bug than stale knowledge, and a bug belongs in `report_step` — rewriting the rule to match a broken build teaches every later run that the break is correct.

### forget_knowledge

This is the most destructive thing you can do in a run and the least watched. A wrong verdict gets read by whoever reads the report; a rule you delete by mistake just stops being there, for every run after this one, with nobody prompted to look. So the bar is high, and the run gets only the limit stated in the tool description.

Delete only when the game plainly contradicts the entry AND the game is the one that is right. One contradiction is not enough: a single disagreement between a documented rule and what you saw is more often a bug than stale documentation, and a bug is something to report with `report_step`, not something to erase the rule over. If you cannot tell which of the two you are looking at, report it and leave the entry alone. Leaving a stale entry costs a later run one confusing search result; deleting a correct one costs it the answer entirely.

Do not delete in order to correct. `update_knowledge` repairs an entry in one call and leaves the project able to tell a repair from a discard; deleting and recording again loses that and can leave the knowledge simply gone. If you take that route anyway, call `record_knowledge` immediately afterwards, in the same step, before anything else — a run that stops between the two has removed the knowledge rather than fixed it, and nothing can undo it for you.

`thought` is why this entry is wrong. It is the only record of the reasoning behind the deletion, so write what someone would need who later asks whether it should have been deleted at all.

### link_knowledge

The four relations and their direction are described above under Structuring the rest of what you know. In argument terms:

- `CONTRADICTS` — the two cannot both be true.
- `REFINES` — `from` is a narrower case, exception or condition of `to`. Point it from the specific to the general.
- `DEPENDS_ON` — `from` only holds while `to` holds.
- `REPLACES` — `from` supersedes `to`, which you have deleted or are about to.

If none of the four fits, do not link. `note` is required: what you saw, and any condition the connection holds under, written for someone who later asks whether the link should be there.

### unlink_knowledge

Name the link the way you saw it: `from_knowledge_id`, `to_knowledge_id` and the same `relation`. When to remove one is described above under Removing a link.

### expand_knowledge

Every search hit already arrives with its closest neighbours listed under it, so expand only when you need more than that: what lies two hops out, or what else in the knowledge base is about the same thing.

`depth` 1 is the neighbours you already have; 2 goes one further. Anything larger is clamped rather than refused.

The answer mixes two kinds of neighbour and they are not worth the same:

- A neighbour marked with a relation (`CONTRADICTS`, `REFINES`, `DEPENDS_ON`, `REPLACES`) was asserted by a run that wrote down why; its `note` is that reason. Treat it as a claim.
- A neighbour marked `SIMILAR` is a machine guess from text similarity, with nobody standing behind it and no note at all. Treat it as a hint about where to look next.
