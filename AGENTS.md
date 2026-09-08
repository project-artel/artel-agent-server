# Project Agent Instructions

## Scope and Precedence

This file is the repository-level entrypoint for coding agents.

Read `.agents/docs/project.md` before non-trivial
work. Repository-specific commands, constraints, and narrower instructions take
precedence over these template defaults.

## Project Workflow

For non-trivial work, follow:

- `.agents/docs/workflow.md`
- `.agents/docs/testing.md`

Coding conventions:

- `.agents/docs/coding-style.md`

For tracked Git work, follow:

- `.agents/docs/issue.md`
- `.agents/docs/commit.md`
- `.agents/docs/pull-request.md`

Use project-local skills when installed and applicable. Skill instructions
define their own triggers, formats, and output paths.

## QA agent structure

`QA_ARCH_LABEL` in `app/agents/qa/arch.py` names the shape of the QA agent, and
every run is filed under it. **Bump it in the same commit that changes that
shape**, and add a paragraph above the constant saying what changed and why, the
way v2, v3 and v4 are explained there.

What counts as a change of shape:

- the tool set, or a tool's argument schema
- the middleware list or its order
- the loop bounds and the per-run allowances in `QaArchSpec`
- **what the model reads on every call** — the view `SceneMemory.render` and
  `PulseMemory.render` produce, and what `fold_stale_scenes` leaves of it

`arch_fingerprint` catches the first three by itself. It hashes nothing about the
fourth: it covers the knobs, the tool signatures and the middleware order, and
the format of the view is in none of them. So a view-format change that skips the
bump files two structures under one name, and two runs that read different
screens land in one bucket with nothing left to separate them.

A prompt version is not this. Prompts are data, carry their own version
directories under `app/prompts/`, and are recorded on the run separately.

`tests/test_qa_arch.py` pins the label beside the fingerprint and the tool list,
so either one moving without the other fails there rather than in a report weeks
later.

Wiring is not improvement, and that test only answers wiring. When you change the
shape, drive a real QA run on this machine with the `qa-run-local` skill in
`.agents/skills/` and put the numbers in the pull request. The fourth item above
needs this most: `arch_fingerprint` hashes nothing about the format of the view,
so a run is the only place a change to it leaves any record at all.

Write down which scenario you ran and how many times, the steps passed out of
total, and **the same two numbers from before the change**. One side's ratio is
not a comparison — alone it reads equally well as a gain, a regression, or noise.
Copy the axes off `run_config` on both sides — model, `prompt_version`,
`agent_arch`, `agent_fingerprint` — with the first two identical across the pair
and the last two differing by exactly the change you made. That is what shows the
difference belongs to your change and not to a prompt version that rode along
with it.

One run is not a result. The model is sampled, so the same configuration on the
same build returns a different step count each time it is asked. If you ran a
cell once, say once in the pull request and claim only what one run supports; a
table of single runs otherwise reads as if it were measured.

Text missing from `qa_log` is not a feature that failed to run. `MAX_LOGGED_CHARS`
in `app/agents/qa/runner.py` clips a tool result before it is stored, and the
`<<scene context>>` block sits at the end of the view, so it is the first thing a
clip takes (ARTEL-824). Counting blocks in `qa_log` and concluding the feature was
dead has already produced one wrong diagnosis. Drive `SceneMemory` in a probe
script when you need to know what the model actually read.

## Terminology in comments, documents, and pull requests

Keep a technical term in English, in backticks, even in the middle of a Korean
sentence: `pulse`, `screen`, `capability`, `anchor`, `branch`, `fold`,
`discriminator`, `evidence`, `wiring`.

Do not invent a Korean substitute for something the code already names. `판독`
for `pulse`, `갈래` for `branch`, `배선` for `wiring`, `판별자` for
`discriminator`, `근거 문서` for an `evidence` document — none of these.

**How common a coinage is in this repository is not an argument for writing
another one.** Several of them are already widespread here. That is history, not
a standard: it means the habit spread before anyone stopped it, and matching it
spreads it further. When you write a new comment, choose the English word even
when the file beside it does not.

The one exception is a sentence you are editing that already uses the old word,
where changing it would leave a single paragraph speaking two ways. Match the
line you are touching; do not convert the file around it as a side errand.

This is not a push toward more English or more Korean. Prose stays whatever
reads naturally. The rule is narrower than that: a thing the code names keeps
the name the code gave it.
