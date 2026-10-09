"""What shape the QA agent has, as data rather than as module constants.

The loop bounds, the per-run allowances, whether the model is shown screenshots
and which middleware wraps the calls are the axes a structural experiment moves.
Left as constants they can only be changed by editing and redeploying, which
makes "compare two agent structures" mean "compare two deployments" — and two
deployments cannot run side by side against the same game.

Versioning follows from that split:

* Prompts are data, so they live in version directories and many versions
  coexist (see ``app/prompts/``).
* Structure is code. Old structures are NOT kept as parallel copies here — an
  unmaintained copy keeps running while the tool signatures around it move on,
  and a comparison against a rotted structure is worse than no comparison,
  because nothing says whether the loss came from the design or from the rot.
  A past structure is identified by its commit and image tag and reproduced by
  redeploying it.
* What survives in the record is therefore an identity, not an implementation:
  ``QA_ARCH_LABEL`` for reading and grouping, ``arch_fingerprint`` for catching
  the case the label misses.

The label is bumped by hand and the fingerprint is derived, because each covers
the other's failure. A label alone goes stale the first time someone changes a
tool and forgets to bump it, and every run after that is filed under a structure
it did not have. A fingerprint alone is unreadable — a report grouped by
``a3f1c9d2e8b0`` says nothing about what that structure was.
"""

import hashlib
import json
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import get_settings
from app.llm.models import LLMModel, get_model_spec

# Bumped by hand when the structure changes in a way worth naming. Reads in
# reports; the fingerprint below is what actually separates the buckets.
#
# v2 because this is the second structure: v1 was one structured LLM call per
# step, replaced wholesale by the tool loop rather than kept beside it.
#
# v3 because `list_scene_capabilities`, `record_capability_verdict` and
# `record_new_capability` (`app/agents/qa/tools/capability_tools.py`) joined the
# tool set: the run can now read and write the content map's `capability` rows,
# which is a different structure even though the loop around it did not change.
#
# v4 because the screen's own text moved into a section of its own, `the screen
# reads:`, drawn on every model call instead of once (`app/qa/pulse.py`). Before
# it, a label the game left on screen was printed the reading it changed and
# never again, and `DEFAULT_KEEP_SCENES = 1` folded that message away on the next
# tool result — so a tutorial line was in front of the model for exactly one call.
# A run before this change and a run after it read different screens from the same
# game.
#
# Nothing the fingerprint hashes moved: the tool set, the tool signatures, the
# middleware order and every knob are the same. That is not this change being
# small, it is `arch_fingerprint` not covering the format of what the model reads
# — see its docstring for what it does cover. The hand-bumped label is the only
# thing in the record that can separate these two structures, which is the case
# the module docstring above says the label exists for.
#
# `screen_capture` (ARTEL-868) opened as an axis without moving this label. The
# default run's shape did not move: the default is `on_demand`, which is what the
# run has always done, and the tool set, the tool signatures and the middleware
# list are all unchanged by the field's arrival. `fold_stale_knowledge` is the
# precedent — `d78e6d4` added it to `QaArchSpec` as a new axis and left the label
# at `v2-tool-loop`. It is a weaker precedent than it looks: that commit also put
# `fold_knowledge_neighbours` into the default middleware list, which AGENTS.md
# does count as a change of shape. This change needs no such licence — the tool
# set, the tool signatures, the middleware list and every knob of the default run
# are provably where they were.
#
# The fingerprint does move, for every structure, because it hashes
# `arch.model_dump()` and that dump gained a key. That is expected and is why
# `_FINGERPRINT_SCHEME` is NOT bumped alongside it: the digests already separate,
# and bumping would only make one structure carry two of them.
#
# The arms of that axis are named in `QaArchSpec.label` rather than here, because
# a deployment runs them all from one image. The specs are checked in at
# `benchmarks/wordventure/arch/` in the workspace. Every arm takes a NEW label
# rather than reusing this constant: `agent_arch` is what `/api/qa-stats` groups
# cells by, so an arm left on the default label shares its cell with every run
# this deployment has ever done.
#
# * `v4-capture-on-demand` — `screen_capture=on_demand`, which is what the
#   default run still does under the label below.
# * `v4-capture-every-call` — the first `every_call` build, which stored the
#   picture in the conversation. Retired. It read 1.3% of its input from cache
#   against the baseline's 97.3% and cost $14.22 against $0.87, because storing a
#   picture per turn means editing an already-sent message to dispose of the last
#   one, and that moves the prefix out from under the cache boundary.
# * `v4-capture-by-role` — `screen_capture=by_role` (ARTEL-870). Two to four
#   pictures a call instead of one, chosen by what the run has done: the current
#   screen, the screen before the last action, and — when there is one — the scene
#   change and the last failure. Same tool set, same middleware; only the number of
#   transient messages moves, so the fingerprint separates it through the knob.
# * `v4-capture-transient` — the same knob after that fix: the picture rides on
#   one request and is never stored. **Same `screen_capture` value, so the same
#   fingerprint** — `arch_fingerprint` hashes the knobs, the tool schemas and the
#   middleware order, and none of those moved. The label is the only thing that
#   separates the two builds in the record, which is the case the module docstring
#   above says the hand-bumped label exists for.
#
# The four arm labels keep their `v4-` names now that the default has moved to
# v5. They are how runs already in `/api/qa-stats` are filed, and renaming them
# here would leave those runs under a name nothing in the source carries.
#
# v5 because every pointer tool now aims with one `target` string instead of
# `x`/`y` (`app/agents/qa/tools/action_tools.py`). `click_at` became `click`,
# `double_click_at` became `double_click`, `drag_pointer` became `drag` with an
# independent target at each end, and `move_pointer` kept its name but not its
# arguments. A target is a screen point, a Unity instance id, or a hierarchy
# selector, and the SDK resolves the last two at the moment the action runs
# rather than when the scene was read. Two things the agent could not do before:
# drag an element the scene gives an id for, and hit a target that moved between
# the observation and the click. Three tool names and four tool schemas moved, so
# the fingerprint moves with the label here.
#
# v6 because `phase_cycle` (below) changes the tool set and a tool schema at every
# value except its default. `in_verdict` gives `report_step` two more arguments,
# `capability_key` and `learned`; `lite` puts the run behind a phase state machine
# and adds `skip_memory_update`; `full` adds `decide_next_action` on top. Each of
# those is a change of shape under the rule in AGENTS.md. The arms are named in
# `QaArchSpec.label`, not here, because one deployment runs all four.
#
# The bump is NOT optional even though the default is `off`, and even though the
# default run's tools, tool schemas, middleware list and every other knob are
# provably where they were. One field added to `QaArchSpec` puts one key into
# `arch.model_dump()`, and `arch_fingerprint` hashes that dump — so the digest of
# EVERY structure moves, `off` included. Measured: the pinned default structure
# hashed `83bc272fee58` before this field and `7235b9a26d43` after it, with nothing
# else touched. `screen_capture` (ARTEL-868) was the same case and did not bump
# the label, on the argument that the default run's shape had not moved. That
# argument does not reach here: at three of this field's four values the default
# run's shape does move, and a label that named only the `off` shape would leave
# `lite` and `full` filed under the name of the free tool loop they replace.
#
# v7 because the default moved from `off` to `lite`, so every run that names
# nothing gets the shape v6 described as opt-in: 38 tools instead of 37
# (`skip_memory_update` joins), `report_step` carrying `capability_key` and
# `learned`, the phase state machine refusing out-of-phase calls, and three more
# system prompt roles. The fingerprint moves with it.
#
# Moved on the record it leaves, not on the score. Over 16 L1 runs on 2026-09-18,
# 8 per arm, `lite` agreed with the answer key on 19.1 of 24 steps against `off`'s
# 18.3 — ranges 16-21 and 12-21, which overlap, so that gap is not a result. What
# is not close: `lite` left 105 `learned` lines and 10 `capability_key` citations,
# `off` left 0 of each, for 13% more cost ($9.89 against $8.76). An `off` run
# finishes knowing exactly what it knew when it started.
#
# **Those runs used an older phase table than the one `phase.py` now holds.** They
# were measured with `ACT` as the only repeating phase; `OBSERVE` and
# `UPDATE_MEMORY` repeat as well now, which removes refusals the measured arm
# paid for. The direction of that change is known — strictly fewer refusals, so
# `lite` costs less and is interrupted less than the numbers above say — but the
# size is not, because it has not been run. The honest reading is that the
# measurement supports "`lite` does not score worse and writes things down", and
# that its cost and refusal figures are an upper bound rather than a reading of
# this table.
#
# v8 because the agent can now read its skills on demand: with `skills=on_demand`
# a `load_skill` tool joins the tool set, and the long sections of the system
# prompt (knowledge base, content map, held state) leave it and are read through
# that tool instead of being resent on every turn. Plan:
# `.plan/general/2026-10-06-slim-the-qa-prompt-and-load-skills-on-demand.md`.
# The new `QaArchSpec.skills` axis defaults to `off`, which is the shape every run
# had before, so the default fingerprint moves only because `arch.model_dump()`
# gained a key (see the v4 note above); the tool set and the middleware list of
# the default run did not change. The label moves anyway because the structure now
# has two shapes and a run must say which side of that axis it was filed on.
#
# This shape was written as `v6-skills-on-demand` on a branch that grew alongside
# the phase cycle, and develop took v6 and v7 for the phase cycle first. No run
# outside local measurement was filed under that name, so it is renumbered to v8
# rather than kept, and v6 keeps meaning the phase cycle. The two axes are
# independent: `skills` and `phase_cycle` can be set in any combination, and the
# phase gate never refuses `load_skill`, because a skill is needed at the moment it
# applies, which can be any phase.
#
# Folding loaded skills is part of the same `on_demand` shape, not a new one. With
# `skills=on_demand` and the `fold_stale_skills` knob on, a middleware of the same
# name keeps the newest loaded skill in full and replaces older ones with a note
# naming the skill to load again (`app/agents/qa/context.py`). It landed together
# with the `skills` axis, so no run was filed under this label without it. The
# default fingerprint moves only because `arch.model_dump()` gained the
# `skills` and `fold_stale_skills` keys; with `skills=off` the middleware list is
# unchanged.
#
# v9 because the model now has six macro tools — `write_macro`, `edit_macro`,
# `read_macro`, `register_macro`, `run_macro` and `resume_macro` — and can send a
# whole sequence of actions in one call instead of one action per turn (ARTEL-914).
# Six tool schemas appeared, so `arch_fingerprint` moves on its own here. A macro
# may loop (`for`, `while`, ARTEL-948) and may hand the turn back at a `checkpoint`
# and carry on through `resume_macro` (ARTEL-949); those landed before any run was
# filed under this label.
#
# The macro grammar does NOT ride on those five descriptions. Each is under the
# 500-character cap `validate_prompts` enforces, and the grammar and the two
# worked examples sit in `qa_run/v19/skill_macro.md`, read through `load_skill`
# when the skills axis is `on_demand` and inlined into the system prompt when it
# is `off`. So the macro text costs a turn only when the model asks for it, which
# is the whole point of the axis above.
#
# This was `v8-macros` until the branch underneath renumbered itself to
# `v8-skills-on-demand` (above). No run was filed under `v8-macros`.
#
# v10 because a phased run with macros on now makes macros part of UPDATE_MEMORY
# (ARTEL-914). `register_macro` moved from ACT to UPDATE_MEMORY in the phase table, so
# registering a macro answers that step the way a knowledge entry does; the three draft
# tools became always-allowed; and the phase directive of such a run gains a paragraph
# (`qa_run/v19/macro_memory_directive.md`) asking, at that step, whether the step had a
# sequence worth saving. None of this moves `arch_fingerprint` — the phase table is not
# hashed and neither is the directive's text — while runs were already filed under
# `v9-macros` (the `macro-ab-l1b` and `macro-ab-l1-nudge` measurements, 2026-10-07). The
# label is the only thing that can tell them apart from runs after this, which is the
# case the module docstring says the hand-bumped label exists for.
#
# v11 because a run with macros on is now handed a macro draft instead of being asked to
# write one (ARTEL-915, ARTEL-916). Every action tool the agent calls by hand is recorded
# in order, with its target lifted to a `selector(...)` or a `find(...)` against the pulse
# memory of that moment, and when a step's verdict is accepted, `report_step`'s answer
# carries a draft of that step's actions that one `register_macro` call keeps. This came
# after `v10-macro-memory` (`macro-ab-l1-memory`, 2026-10-08) left B with zero macros in
# six runs, as `v9-macros` had in twelve. Nothing hashed moves: the tool schemas are
# unchanged and the draft lives in a tool's answer.
#
# v12 because a run with macros on now reads a list of the macros earlier runs registered on
# this build, once, at the end of the scenario's opening message (ARTEL-934, ARTEL-935).
# Until then a run could call a registered macro only if it already knew the name, and
# nothing told it one existed, so what run 1 registered was unreachable by run 2. The list
# is one line per macro, at most 12 (`MAX_MACROS_IN_FIRST_MESSAGE`), with a note saying how
# many were cut. It is drawn there rather than under the scene view or at the tail of every
# call because the opening message is fixed when the run starts, so the prompt prefix does
# not change on later turns (ARTEL-621). A draft's first comment now names the step it was
# drafted for, since that comment is the line the next run reads. A run with `macros=off`
# or a build with no macros reads exactly what it did under v11. Nothing hashed moves: the
# tool schemas are unchanged and the list is part of a message, not of a tool.
#
# v13 because `register_macro` is no longer refused outside UPDATE_MEMORY (`ANY_PHASE` in
# `tools/phase.py`). Called during UPDATE_MEMORY it still answers that phase; called in any
# other phase it runs and moves no phase. Under v12, try 62 of `macro-continuity-l1`
# (2026-10-08) wrote a macro during ACT, had its registration refused, never retried it in
# UPDATE_MEMORY, and the draft was dropped with the run. The macro paragraph of the phase
# directive says so in one more sentence. Nothing hashed moves: the phase table is not
# hashed and neither is the directive's text.
#
# v14 because the macro draft and what the agent is asked about it changed (`macro-continuity-l1`,
# 2026-10-08: 34 drafts offered, 1 registered). A run of key presses that ended in a scene
# change is drafted as `while scene() == "<scene>":` instead of the exact count, since the
# action recording now keeps the scene after each call (`DispatchRecord.scene_after`). Drafts
# are offered only for passed verdicts. The offer states that the scenario runs again and the
# next run is shown this build's registered macros, and gives three answers — register, fix
# with `edit_macro` then register, or decline by name — and an offered draft counts as read so
# `edit_macro` accepts it. `skip_memory_update` refuses once a reason that does not name a
# pending draft. The macro paragraph says the scenario runs again. Nothing hashed moves: the
# tool schemas are unchanged.
#
# v15 because the macro draft gets a phase of its own, and with it a tool. Under v14 one
# `skip_memory_update` still answered both "what did this step teach about the game" and "keep
# this draft", and in v14 try 80 the agent read the draft and the fact that the next run sees
# registered macros and still declined. A step whose passing verdict offers a draft now goes
# VERIFY -> REVIEW_DRAFT -> UPDATE_MEMORY; REVIEW_DRAFT is answered by `register_macro` or the
# new `decline_macro_draft` (name and what replaying it would do wrong), and a refused
# registration keeps it open. Steps without a draft go VERIFY -> UPDATE_MEMORY as before, and
# `skip_memory_update` is back to meaning only "no knowledge to write" (v14's one-time
# refusal is gone). The offer makes registering the default for a passed step and states the
# cost of not registering in tool calls. `decline_macro_draft` joins the default tool set, so
# `arch_fingerprint` moves.
#
# v16 because a run with macros on is now told that macros are how it operates and the
# action tools fill in (ARTEL-917, asked for on 2026-10-08). In `macro-continuity-l1-v14`
# five of six first runs carried macros to their second run, but 6 of the 14 `run_macro`
# calls in the second runs were refused for coming outside ACT and one second run carried
# two macros and ran none. The macro paragraph now opens with the order for each step — a
# registered macro first, then a macro written for any sequence, then hand actions only for
# single presses, looks and recovery — `report_step`'s answer names the registered macro for
# the next step, and a `run_macro` refused in UPDATE_MEMORY or REVIEW_DRAFT says to make it
# the next step's first action. The action tools' descriptions are unchanged because a run
# with macros off reads them too. Nothing hashed moves.
#
# v17 because stale pulse views are now folded out of what the model reads (asked for on
# 2026-10-08). Measured from LangSmith traces of complete 24-step L1 runs: the last model call
# of sol try 138 was 351,671 prompt tokens (1,065,132 chars, 276 messages), and the pulse view
# text inside tool results was 89.8% of those chars (`run_macro` x15 alone 38%); luna try 131
# ended at 158,141 tokens with the pulse view at 87.0%. Input grew linearly, 13k -> 158k -> 352k
# tokens within try 138, and compaction, which triggers at 90% of a 922k window, never fired.
# The cause: `fold_stale_scenes` matched only the `<<scene view N>>` markers, while a build that
# sends only `pulse` gets the pulse-only view, which ARTEL-621 had left unfolded on purpose (the
# runner's `views folded=0 kept=0` on every call). ARTEL-621 had two reasons, and each now has an
# answer. First, folding the newest-but-one view on every call rewrote the middle of the prompt
# every call, and with the single cache breakpoint at the end of the prompt nothing cached past
# the first message. The fold now runs in batches: nothing is folded until more than
# `DEFAULT_MAX_FULL_VIEWS` (8) full views have piled up, then all but the newest are folded at
# once, so the cache is rewritten once per 8 calls and the calls between only append. Second,
# the pulse view is a delta that does not redraw a value it has already shown, so folding the
# old views loses values that moved once and then stayed put. When a batch is folded, the runner
# now calls `PulseMemory.redraw_all_values_next`, and the first pulse view after the fold draws
# every held value again, marked `(changed earlier)`. A folded view becomes one line naming its
# reading and scene. Scene views follow the same batching. Nothing hashed moves: the middleware
# list, the tools and the knobs are unchanged.
#
# v18 because a `run_macro` or `resume_macro` answer now carries one pulse view instead of one
# per action (asked for on 2026-10-09). The macro runner sent each action through
# `ToolContext.act`, which drew a pulse view onto every action's sentence, and `_render` listed
# those sentences under "What the game said:" before the answer drew one more. In the first v17
# run, try 143, a 30-action `advance_opening_dialogue` came back with 31 views in one 17k-char
# answer, so the first macro alone crossed `DEFAULT_MAX_FULL_VIEWS` and the batch fold ran at
# the 11th call with 32 views folded. Nothing read those views but the runner, which looks at
# `ActionOutcome`'s data, not its text. The macro host now calls `act(screen=False)`: each
# action's line keeps its outcome and any operator message, and the one view drawn when the
# macro stops or ends covers every reading since the last answer. Nothing hashed moves.
#
# `pulse_relevance` (ARTEL-958) opened as an axis without moving this label, on
# the `screen_capture` argument above. At its default `False` the tool set, the
# tool schemas, the middleware list and every other knob are where they were, and
# the view is byte-identical because `PulseMemory.relevance` stays empty — a test
# in `tests/test_qa_pulse_cosmetic.py` holds that. At `True` the view moves, which
# the fingerprint cannot see, so each arm carries its own label in
# `benchmarks/wordventure/arch/pulse-relevance-*.json`.
QA_ARCH_LABEL = "v18-macro-one-view"

# Which facts the fingerprint is computed from. Bump when that set changes, so
# a digest from the old scheme is never mistaken for one from the new.
_FINGERPRINT_SCHEME = 1

# The fixed part of the tool-call budget: the opening observation, `finish_run`,
# and the headroom between them. The per-run allowances are added on top of it
# rather than folded into it — see `ResolvedArch.tool_call_limit`.
#
# Set to a ceiling no real run reaches. Both bounds are runaway guards now, not
# a ration: a run cut off mid-scenario reports nothing, which is the one outcome
# worse than a slow run. A caller that wants a bounded structure sends its own.
BASE_TOOL_CALLS = 1_000
TOOL_CALLS_PER_STEP = 1_000
RUN_DEADLINE_SECONDS = 86_400.0

# --- how much of the game and the knowledge base one run may move -------------
#
# These live here, with the other knobs, rather than beside the tools that spend
# them. They stopped being facts about those tools the moment a run could be
# asked to use different ones: two runs that differ only in how much they may
# look up are two structures, and the number has to be somewhere the fingerprint
# can see it. `knowledge.py` and `vision.py` re-export them under their old names
# and keep the prose about what each tool is for, which is still theirs.
#
# Every default below is a ceiling no real run reaches. The numbers used to be a
# ration — each sized against the argument that a run which keeps looking things
# up never decides — and what they produced in practice was runs that stopped
# mid-scenario with the question unanswered. Rationing is left to the tool
# descriptions, which still say what each tool is and is not for.
#
# They stay as `QaArchSpec` fields rather than being deleted: a structural
# experiment can still ask for a rationed run, and the fingerprint still has to
# separate one that did from one that did not.
MAX_CAPTURES_PER_RUN = 1_000_000
MAX_SEARCHES_PER_RUN = 1_000_000
MAX_RECORDS_PER_RUN = 1_000_000
MAX_FORGETS_PER_RUN = 1_000_000
MAX_LINKS_PER_RUN = 1_000_000
MAX_UNLINKS_PER_RUN = 1_000_000
MAX_EXPANDS_PER_RUN = 1_000_000
MAX_ISSUES_PER_RUN = 1_000_000


class VisionMode(StrEnum):
    """Whether the run shows the model screenshots.

    ``auto`` follows the model's own capability, which is what every run did
    before this was a choice. ``off`` is what makes "the same model with and
    without vision" a comparison one deployment can run.
    """

    auto = "auto"
    on = "on"
    off = "off"


class ScreenCaptureMode(StrEnum):
    """When a screenshot of the running game reaches the model.

    ``on_demand`` is what every run did before this was a choice: the model calls
    `capture_screen` and the picture arrives on the next model call. ``every_call``
    puts a fresh one in front of the model on every call it did not already ask for
    a whole-screen picture on.

    An enum rather than a bool for two reasons. `on_change` — capture only when
    the screen actually moved — is the next value this axis is expected to take,
    and the string lands verbatim in `run_config`, where `screen_capture=every_call`
    reads in a report and `auto_capture=true` does not.

    ``by_role`` is ``every_call`` with more than one picture. It carries the current
    screen, the screen as it was just before the last action, and — when there is
    one — the screen at the last scene change and at the last failure. Two, three
    or four, decided by what the run has done rather than by the model. The pilot
    that motivates it is in `ARTEL-868`: matched against the labelled steps, the
    `every_call` arm caught one of L1's six deliberately-refused steps and the
    baseline caught none and two. One picture says what is on screen; most QA
    verdicts turn on what *changed* when something was pressed, and that needs the
    pair.

    `every_call` does NOT remove `capture_screen` from the tool set. Removing it
    would make the arm two changes rather than one: the picture arriving unasked,
    and the loss of the cropped close-up of a single element that the tool's
    `target_id` gives. It would also strand the vision directive
    (`app/prompts/qa_run/*/vision_directive.md`), which names the tool, and fixing
    that would move `prompt_version` — an axis the comparison needs held still.
    """

    on_demand = "on_demand"
    every_call = "every_call"
    by_role = "by_role"


class PhaseCycleMode(StrEnum):
    """How much of `OBSERVE -> DECIDE -> ACT -> VERIFY -> UPDATE_MEMORY` the run is held to.

    Not a bool, because the four values are a cost ladder measured in extra model
    calls per step: 0, 0, +1, +2. A bool cannot express "ask the same question
    without spending a round trip on it", and that is the rung worth measuring
    first — if it works, the two expensive ones need not be bought at all.

    ``off`` is the free tool loop every run has done until now, and its being the
    default is a condition rather than a preference. ARTEL-667 sent the run back
    once from `finish_run` and eighteen tests that close a run broke; this axis
    sends it back at every step. Every test that does not name a value here has to
    keep costing exactly the round trips it costs today.

    ``remember_in_verdict`` spends no extra model call: `report_step` takes
    `capability_key` and `learned`, so the `UPDATE_MEMORY` question is answered in
    the same turn as the verdict. What it addresses is measured — 27 stage runs on
    prompt v15 made 1,566 tool calls, of which `record_capability_verdict`,
    `record_new_capability` and `list_scene_capabilities` were called 0 times each,
    with the prompt, the closing asks and the compaction ledger all asking for them
    in words. An argument is not another sentence.

    ``lite`` lifts `UPDATE_MEMORY` into a turn of its own behind a phase state
    machine, which answers an out-of-phase call with a refusal in the tool result.
    ``full`` adds `DECIDE` on top, at one more model call per step.

    The refusal is a tool result and nothing else. Narrowing the tool list per call
    — what `ModelRequest.override(tools=...)` allows — is deliberately not done:
    tool declarations sit at the front of a request, so a list that rotates per
    phase moves the prompt prefix every turn. Breaking that prefix in a weaker way
    has been measured at cache reads of 1.3% against 97.3% and $14.22 against $0.87
    for one run (ARTEL-868).
    """

    off = "off"
    remember_in_verdict = "in_verdict"
    lite = "lite"
    full = "full"

    @property
    def remembers_in_verdict(self) -> bool:
        """Whether `report_step` carries `capability_key` and `learned`.

        True from `in_verdict` upwards. `lite` and `full` keep the arguments even
        though they also have `skip_memory_update`: the two ask different things —
        one is the verdict citing the row it checked, the other is the run saying
        what it is leaving behind — and dropping the arguments at the upper rungs
        would make `lite` minus `in_verdict` two changes instead of one.
        """
        return self is not PhaseCycleMode.off

    @property
    def gates_phases(self) -> bool:
        """Whether out-of-phase tool calls are refused. `lite` and `full` only."""
        return self in (PhaseCycleMode.lite, PhaseCycleMode.full)

    @property
    def decides_in_its_own_turn(self) -> bool:
        """Whether `decide_next_action` is in the tool set. `full` only."""
        return self is PhaseCycleMode.full


class QaArchError(ValueError):
    """The requested structure cannot be built for the requested model."""


class QaArchSpec(BaseModel):
    """A requested structure. Every field optional; the defaults are today's run.

    The ceilings are runaway guards, not rations: they sit far above what any
    scenario asks for, and exist so a malformed request cannot ask for an
    unbounded call budget or a deadline that never fires.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(default=QA_ARCH_LABEL, max_length=50)
    base_tool_calls: int = Field(default=BASE_TOOL_CALLS, ge=1, le=1_000_000)
    tool_calls_per_step: int = Field(default=TOOL_CALLS_PER_STEP, ge=1, le=1_000_000)
    deadline_seconds: float = Field(default=RUN_DEADLINE_SECONDS, gt=0, le=86_400)
    max_searches_per_run: int = Field(default=MAX_SEARCHES_PER_RUN, ge=0, le=1_000_000)
    max_records_per_run: int = Field(default=MAX_RECORDS_PER_RUN, ge=0, le=1_000_000)
    max_forgets_per_run: int = Field(default=MAX_FORGETS_PER_RUN, ge=0, le=1_000_000)
    max_links_per_run: int = Field(default=MAX_LINKS_PER_RUN, ge=0, le=1_000_000)
    max_unlinks_per_run: int = Field(default=MAX_UNLINKS_PER_RUN, ge=0, le=1_000_000)
    max_expands_per_run: int = Field(default=MAX_EXPANDS_PER_RUN, ge=0, le=1_000_000)
    max_captures_per_run: int = Field(default=MAX_CAPTURES_PER_RUN, ge=0, le=1_000_000)
    max_issues_per_run: int = Field(default=MAX_ISSUES_PER_RUN, ge=0, le=1_000_000)
    vision: VisionMode = VisionMode.auto
    screen_capture: ScreenCaptureMode = ScreenCaptureMode.on_demand
    # `off` is the compatibility value: at it the tool set, every tool schema, the
    # middleware list and every other knob are what they were before this axis
    # existed. It is no longer the default, so that shape has to be asked for by
    # name now — including by a run pinned to a prompt version older than `v18`,
    # which carries no `phase_directive` for the gate's refusals to rest on and is
    # refused in `resolve_run_config` rather than gated in silence.
    phase_cycle: PhaseCycleMode = PhaseCycleMode.lite
    # `off` keeps every skill inlined in the system prompt, as before. `on_demand`
    # moves the skills behind the `load_skill` tool, which then joins the tool set.
    skills: Literal["off", "on_demand"] = "off"
    # The six macro tools and the `macro` skill. `off` is the shape every run had
    # before macros (ARTEL-926): no macro tool in the list and no `macro` skill in
    # the prompt or behind `load_skill`, so an A/B on this axis differs by exactly
    # the macros. Withholding the skill matters as much as withholding the tools —
    # left in, the Skills section would tell the `off` arm to load something
    # "before your first `write_macro`", a tool it does not have.
    macros: Literal["off", "on"] = "on"
    fold_stale_scenes: bool = True
    # Folds the neighbour blocks the search volunteers, and only those (ARTEL-277).
    # Separate from `fold_stale_scenes` because the two are independently useful
    # to turn off: what they fold is recovered by different tools out of
    # different budgets.
    fold_stale_knowledge: bool = True
    # Folds every loaded skill but the newest, and only when `skills` is
    # `on_demand`: with `off` the skills are in the system prompt and there is
    # nothing loaded to fold. Separate from the two folds above because what it
    # folds is recovered by a third tool, `load_skill`.
    fold_stale_skills: bool = True
    # `pulse` view 에서 cosmetic member 를 숨긴다(ARTEL-958). 켜면 run 이 member 타입마다
    # Jev decision model 에 gameplay state 인지 묻고(`app/qa/relevance.py`), 확실히 아니라고
    # 답한 member 를 `PulseMemory.render` 가 빼고 개수만 적는다.
    #
    # 기본값이 `False` 인 이유: 끄면 `PulseMemory.relevance` 가 비고, 그때 뷰는 이 axis 가
    # 생기기 전과 byte 단위로 같다. 그래서 기본 run 의 모양이 안 움직이고 `QA_ARCH_LABEL` 도
    # 그대로다 — `screen_capture` 와 같은 경우다. 두 arm 의 label 은 workspace 의
    # `benchmarks/wordventure/arch/pulse-relevance-*.json` 이 붙인다.
    pulse_relevance: bool = False
    # Compaction rewrites what the model reads once a run grows past a fraction of
    # its context, so a run with it and a run without it are two agents even with
    # the same tools. `None` defers to the deployment's own setting, which is what
    # every caller that does not care should send.
    compaction: bool | None = None
    compaction_trigger_fraction: float | None = Field(default=None, gt=0, le=1)
    compaction_keep_messages: int | None = Field(default=None, ge=1, le=200)
    compaction_min_new_messages: int | None = Field(default=None, ge=1, le=100)
    compaction_trim_tokens: int | None = Field(default=None, ge=0, le=200_000)

    @model_validator(mode="after")
    def forgets_need_records(self) -> "QaArchSpec":
        """Deleting without being able to write the replacement loses knowledge.

        `app/agents/qa/knowledge.py` exempts a replacement write from the record
        cap precisely so this cannot happen mid-run; a spec with deletions and no
        records would put the hole back before the run even starts.
        """
        if self.max_forgets_per_run > 0 and self.max_records_per_run == 0:
            raise QaArchError(
                "max_forgets_per_run > 0 requires max_records_per_run > 0: a "
                "deletion whose replacement cannot be written loses knowledge."
            )
        return self

    @model_validator(mode="after")
    def graph_tools_need_searches(self) -> "QaArchSpec":
        """An id can only be linked, unlinked or expanded after a search showed it.

        Both graph tools refuse an endpoint the run has not been shown, and the
        only things that show one are a search and the neighbours that ride along
        with it. A spec that allows them with no searches therefore enables tools
        that can never legally be called — the run pays for them in its tool-call
        ceiling and can never spend them.

        Unlike `forgets_need_records` this asks for no paired write: withdrawing a
        link is not something that leaves a hole somebody has to fill.
        """
        graph_calls = (
            self.max_links_per_run + self.max_unlinks_per_run + self.max_expands_per_run
        )
        if graph_calls > 0 and self.max_searches_per_run == 0:
            raise QaArchError(
                "max_links_per_run / max_unlinks_per_run / max_expands_per_run "
                "require max_searches_per_run > 0: an entry the run was never "
                "shown can be neither linked nor expanded."
            )
        return self


# The skill that exists only for the macro tools. Named here, beside the axis that
# decides whether the run has them.
MACRO_SKILL = "macro"


def withheld_skills(arch: "QaArchSpec | ResolvedArch") -> frozenset[str]:
    """Skill names this structure must not see, though the prompt version has them.

    One place, because three readers list skills — the inlined sections, the Skills
    section and `load_skill` — and one of them forgetting would leave a line about
    a tool the run does not have.
    """
    return frozenset() if arch.macros == "on" else frozenset({MACRO_SKILL})


DEFAULT_ARCH = QaArchSpec()


class ResolvedArch(BaseModel):
    """A spec with nothing left to decide. `vision` is now a fact, not a wish."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    base_tool_calls: int
    tool_calls_per_step: int
    deadline_seconds: float
    max_searches_per_run: int
    max_records_per_run: int
    max_forgets_per_run: int
    max_links_per_run: int
    max_unlinks_per_run: int
    max_expands_per_run: int
    max_captures_per_run: int
    max_issues_per_run: int
    vision: bool
    screen_capture: ScreenCaptureMode
    phase_cycle: PhaseCycleMode
    skills: Literal["off", "on_demand"]
    macros: Literal["off", "on"]
    fold_stale_scenes: bool
    fold_stale_knowledge: bool
    fold_stale_skills: bool
    pulse_relevance: bool
    compaction: bool
    compaction_trigger_fraction: float
    compaction_keep_messages: int
    compaction_min_new_messages: int
    compaction_trim_tokens: int

    def tool_call_limit(self, steps: int) -> int:
        """Two bounds exist because either alone leaves a hole: a call cap alone
        lets one unanswered call hold the run open, a clock alone lets a fast
        loop burn budget. This is the call half; `deadline_seconds` is the clock.

        The allowances are added to the base rather than taken out of the steps.
        Left inside, `search_knowledge` would have spent its budget on the
        scenario and shortened every run by however much it looked things up.
        Captures are deliberately NOT added: the capture budget has always come
        out of the step allowance, and folding it in here would quietly widen
        every existing run's ceiling. Issue reports are not added for both
        reasons at once — a report is made about the step being judged, so it
        belongs in that step's allowance, and adding it here would widen the
        ceiling of every run including the ones that never file one.
        """
        base = (
            self.base_tool_calls
            + self.max_searches_per_run
            + self.max_records_per_run
            + self.max_forgets_per_run
            + self.max_links_per_run
            + self.max_unlinks_per_run
            + self.max_expands_per_run
        )
        return base + self.tool_calls_per_step * max(steps, 1)


def _resolved(spec: QaArchSpec, vision: bool) -> ResolvedArch:
    """Fill in every field the spec left to the deployment.

    The compaction knobs default to `None` rather than to numbers because their
    real default is operational — it lives in `Settings` and is tuned per
    environment. Copying those numbers here would be a second source of truth,
    and the copy is the one that goes stale.
    """
    settings = get_settings()
    chosen = spec.model_dump()
    deferred = {
        "compaction": settings.qa_compaction_enabled,
        "compaction_trigger_fraction": settings.qa_compaction_trigger_fraction,
        "compaction_keep_messages": settings.qa_compaction_keep_messages,
        "compaction_min_new_messages": settings.qa_compaction_min_new_messages,
        "compaction_trim_tokens": settings.qa_compaction_trim_tokens,
    }
    for field, fallback in deferred.items():
        if chosen[field] is None:
            chosen[field] = fallback
    return ResolvedArch(**{**chosen, "vision": vision})


@lru_cache(maxsize=1)
def default_resolved_arch() -> ResolvedArch:
    """The structure a caller gets by asking for nothing, with vision on.

    A convenience for callers that build tools outside a run — the tools do not
    read the compaction knobs, and a real run always resolves against its model.
    """
    return _resolved(DEFAULT_ARCH, vision=True)


def resolve_arch(spec: QaArchSpec, model: LLMModel) -> ResolvedArch:
    """Settle `vision` against what the model can actually do.

    A requested `on` that the model cannot honour is refused rather than
    downgraded. Silently running text-only under a spec that says vision would
    file the run in the wrong bucket, and a comparison built on that is wrong in
    the one direction nobody checks.
    """
    supports_vision = get_model_spec(model).supports_vision
    if spec.vision is VisionMode.on and not supports_vision:
        raise QaArchError(
            f"Model '{model.value}' cannot read images, so vision='on' cannot be "
            f"honoured. Use 'auto' to follow the model, or 'off' to state it."
        )
    vision = supports_vision if spec.vision is VisionMode.auto else spec.vision is VisionMode.on
    # Against the settled `vision`, not against `spec.vision`. The case that
    # matters is `vision='auto'` on a model that cannot read images, which is only
    # False after the line above. Checked here rather than in a `model_validator`
    # for the same reason: the spec alone does not know.
    #
    # Refused rather than downgraded, exactly as `vision='on'` is.
    # `middleware_names_for` only wires `capture_vision` when vision is on, so a
    # downgrade would leave a run labelled `every_call` running with no pictures at
    # all — the wrong-bucket failure the refusal above exists to prevent.
    if spec.screen_capture is not ScreenCaptureMode.on_demand and not vision:
        raise QaArchError(
            f"screen_capture='{spec.screen_capture.value}' needs vision, and this run "
            f"resolved to vision off (model '{model.value}', "
            f"vision='{spec.vision.value}'). Use screen_capture='on_demand', or a "
            f"model that reads images."
        )
    return _resolved(spec, vision)


def arch_fingerprint(
    arch: ResolvedArch, tools: list, middleware_names: tuple[str, ...]
) -> str:
    """A digest of the structure, and of nothing else.

    Model, prompt version and language are deliberately excluded. They are
    separate comparison axes, and a digest that moved with them could not group
    "the same structure under two models" — which is the comparison this exists
    to make possible.

    Tool argument schemas are in because a tool whose signature changed is a
    different tool to the model: same name, different affordance, different run.
    """
    facts = {
        "scheme": _FINGERPRINT_SCHEME,
        "kind": "create_agent-tool-loop",
        # `label` is excluded for the same reason the model is: it is a name, not
        # a structure. Hashing it would mean a rename split one structure into two
        # buckets, and the pair of records could no longer show that the rename
        # changed nothing.
        "arch": arch.model_dump(mode="json", exclude={"label"}),
        "tools": sorted(
            (tool.name, json.dumps(tool.args, sort_keys=True, default=str))
            for tool in tools
        ),
        # Order matters: middleware wraps in sequence, so a reordering is a
        # different call path even with the same members.
        "middleware": list(middleware_names),
    }
    digest = json.dumps(facts, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(digest.encode()).hexdigest()[:12]


@lru_cache(maxsize=32)
def structure_of(arch: ResolvedArch) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    """Tool names, middleware names and the fingerprint for one structure.

    The fingerprint has to be known when the session opens, before any channel
    exists, so the tools are built here against throwaway wiring. Only their
    names and schemas are read — `build_tools` defines closures and touches
    nothing — and building them from the real builder is the point: a signature
    that changed in `tools.py` moves the digest without anyone remembering to
    update a second list.

    Cached because the answer depends only on the structure, and a run should not
    pay to rediscover it.
    """
    # Imported here: `tools` imports this module's siblings, and at module scope
    # this would close the import cycle.
    from app.agents.qa.compaction import build_compact_tool
    from app.agents.qa.runner import middleware_names_for
    from app.agents.qa.tools import QaRunState, build_tools

    state = QaRunState(total_steps=0)
    tools = build_tools(_ThrowawayChannel(), state, arch)
    # The compaction middleware carries its own tool rather than registering it in
    # `build_tools` — a `compact_context` that sets a flag nothing reads is worse
    # than an absent one. It still has to be counted here: what the agent can call
    # is part of what the agent is.
    if arch.compaction:
        tools = tools + [build_compact_tool(state)]
    # `load_skill` needs no line here: `build_tools` registers it itself when
    # `arch.skills` is `on_demand`, so the fingerprint reads its real schema.
    names = tuple(tool.name for tool in tools)
    middleware = middleware_names_for(arch)
    return names, middleware, arch_fingerprint(arch, tools, middleware)


class _ThrowawayChannel:
    """Stands in for a `QaRunChannel` while tool schemas are read off.

    The tools close over a channel but their schemas do not depend on it, and
    nothing is called here. Any attribute access would be a bug in this module,
    so it raises rather than returning a plausible-looking stub.
    """

    def __getattr__(self, name: str):
        raise AssertionError(
            f"structure_of() must not invoke the channel (asked for {name!r})."
        )
