---
name: qa-run-local
description: >-
  Drive a real QA run against a local ARTEL stack, from the agent-server side.
  Use when a change here needs an answer a unit test cannot give — a
  `QA_ARCH_LABEL` bump, a change to what the model reads on every call, a
  prompt version, a middleware or tool change — and the question is whether the
  agent plays better, not whether the code is wired. Also use when the user
  says "로컬에서 QA 런 돌려줘", "agent 성능 측정", "2x2 측정", "게임 띄워서
  확인", "벤치마크 돌려줘".
---

# Local QA run — agent-server view

**The canonical procedure is the same skill one level up, in the workspace that
holds this repository — `../.claude/skills/qa-run-local/SKILL.md` from the
repository root — not this file.** It carries the whole stack: PostgreSQL,
Redis, MinIO, orchestration-server, the Unity build and its
`ArtelBenchBuild.cs`, running several games at once, and seeding
`benchmarks/wordventure/`. Read it before standing anything up. This file exists
because skills are discovered per directory, so someone who opened only
`artel-agent-server` never sees that one — it holds the few facts that are about
this repository, and it is a copy: **when you change either file, change both.**

## Before spending a run

```bash
# artel-agent-server/.env
ORCHESTRATION_BASE_URL="http://localhost:8091"   # the INTERNAL port, not 8090
```

Miss it and **the run starts fine and measures nothing.**
`fetch_scene_context` (`app/qa/scene_context.py`) returns `None` for every
failure there is — unset base URL, wrong port, 404, timeout — and logs a warning
instead of raising, so the `<<scene context>>` block never reaches the model: a `content_map_mode=frozen`
arm becomes byte-identical to an `off` arm, and `llm_usage` stays empty so cost
cannot be read either. Nothing complains, which is why this is the one to check
first. The two orchestration-side settings that fail the same quiet way are in
the canonical file.

## Holding the axes

`app/qa/run_config.py` resolves what a run was, and `runner.py` writes it onto
the try as `run_config`. Read the arms out of that column, never out of memory
or out of the `label`:

- `model`, `provider`, `reasoning` — the model axis
- `prompt_version` and `prompt_hashes` — resolved versions, not the alias
- `agent_arch` (`QA_ARCH_LABEL`) and `agent_fingerprint`
- `arch`, `tools`, `compaction_model`, `compaction_prompt_version`

A structure comparison holds every one of these fixed except the two the change
moves. If a prompt version rode along with an `agent_arch` bump, the pair of
numbers is not about the structure.

Two run rules that decide whether repetition means anything:

- **Relaunch the game between arms.** A finished run leaves the game where it
  stopped, so the next arm's "observe the title screen" starts from a battle
  scene. That difference is not the arm.
- **`frozen`, not `on`.** `on` lets a run write `verdict` rows and `knowledge`
  back, so the second run of an arm reads what the first one left behind.

## Reading the result

```sql
SELECT qr.id, qr.label, qt.status, qt.steps_passed || '/' || qt.steps_total,
       qt.completed_at - qt.started_at AS took,
       qt.run_config->>'agent_arch', qt.run_config->>'agent_fingerprint',
       qt.run_config->>'prompt_version'
FROM qa_run qr JOIN qa_try qt ON qt.qa_run_id = qr.id ORDER BY qr.id;
```

`qa_try.status = FAILED` is a verdict, not a crash — read the closing `STATUS`
frame in `qa_log` for what the agent said about itself.

**Do not conclude a feature did not run from text missing in `qa_log`.**
`MAX_LOGGED_CHARS = 4000` in `app/agents/qa/runner.py:48` clips a tool result
*before* it is stored, so the row holds the same truncated copy the console
showed. The `<<scene context>>` block is appended at the **end** of the view, so
it is the first thing a clip removes, and a large scene's block cannot fit at
all (ARTEL-824). This misreading has already happened once. The compaction
ledger is worse — it never reaches `qa_log`, because `_TURN_PRODUCING_NODES`
skips middleware nodes. To see what the model actually read, drive `SceneMemory`
directly in a probe script; that path is not clipped.

**One run is not a result.** Sampling makes the same configuration on the same
build give a different step count each time. Say how many repetitions a cell
got, and claim only what that many supports.

## After the run

Two places, both required:

- The pull request, per `## QA agent structure` in `AGENTS.md` — before and
  after, the held axes, and the repetition count.
- Notion, per the canonical skill's `## Record it in Notion`. It lists what a
  write-up has to carry to be readable next month; a terminal scrollback is not
  a record.
