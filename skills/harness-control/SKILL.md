---
name: harness-control
description: "Drive the 4-stage dev harness (spec emit → segmented build → swarm transport → review/merge) from Archie's own agent loop — the same pipeline an external SSH actor runs today, callable as skills instead of raw shell commands."
version: 1.0.0
author: Morley (FSFAI)
license: proprietary
platforms: [linux]
metadata:
  hermes:
    tags: [Harness, Build, Swarm, Transport, Merge, Orchestration]
prerequisites:
  profile: dev            # MUST be /profile dev — needs the LOCAL terminal (GitHub key + ~/harness)
  commands: [python3, git, bash]
---

# harness-control — the 4-stage dev harness as in-agent skills

These four scripts wrap the existing harness pipeline so Archie's own agent loop (reached via the
dashboard's embedded chat, Telegram, or an SSH session) can drive the same stages an external actor
runs over SSH today. They are deliberately THIN: they resolve paths, take the shared DO lock, shell
out to the existing, unchanged code path, and propagate the underlying script's outcome (including
aborts/alerts) back to the caller instead of swallowing it.

```
Stage 1  run_stage1.py   → emit_spec.py        → harness/spec/<id> branch on origin
Stage 2  run_stage2.py   → stage2_build.py     → build/<id> branch (segmented, forced-Fusion)
Stage 3  run_stage3.py   → transport.sh        → fix/<id> branch on origin (swarm run on Hetzner)
Stage 4  run_stage4.py   → (this skill itself)  → merge fix/<id> into main, drop _harness/
```

> **You must be on `/profile dev` (or `hivemind`).** Every stage needs the LOCAL terminal (GitHub
> deploy key + ~/harness). The casual `default` profile is sandboxed and cannot push. If Morley
> starts this on `default`, tell him to send `/profile dev` first.

## Concurrency (do-lock.sh)

Every stage goes through the shared `do-lock.sh` — mode `build` for Stage 2/3 (long mutations),
`attend` for Stage 1/4 (shorter attended operations). A busy lock surfaces as a clear "already
running" message (exit 75 / EX_TEMPFAIL), not a crash. This means a chat-triggered run cannot
collide with a Telegram- or SSH-triggered one.

## Stage 1 — emit a spec bundle

```bash
run_stage1.py --run-id YYYY-MM-DD-slug \
    --spec build-spec.md --prompt build-prompt.md --manifest checkpoint-manifest.json \
    [--kb KB_DIR] [--no-push] [--profile NAME] [--dry-run]
```

Wraps `emit_spec.py`. Takes a spec bundle (three files + optional kb/) and pushes it as a
`harness/spec/<run-id>` branch that Stage 2 consumes.

## Stage 2 — segmented build

```bash
run_stage2.py --run-id YYYY-MM-DD-slug [--base main] [--model M] \
    [--preset budget|full] [--max-segments N] [--profile NAME] [--dry-run]
run_stage2.py --smoke [--dry-run]     # synthetic 1-checkpoint build (cheap)
```

Wraps `stage2_build.py` — the driver that sends Hermes through a SEGMENTED build on a
`build/<run-id>` branch, FORCING a Fusion council consult at each checkpoint and injecting the
synthesis back into the same session lineage.

## Stage 3 — transport to Hetzner swarm

```bash
run_stage3.py --run-id YYYY-MM-DD-slug [--routes ROUTES_CSV] [--waves N] \
    [--baseline origin/main] [--dry-run]
```

Wraps `transport.sh` — ships `build/<run-id>` to the Hetzner swarm box, runs the swarm, fetches
`fix/<run-id>` back, and pushes it to origin for Stage 4. `transport.sh` already handles waking
the box, gating the push on the swarm's real outcome, and alerting Morley on abort.

## Stage 4 — review + merge (two-step gate)

Stage 4 has NO existing underlying script — `run_stage4.py` IS the script. It uses a two-step
dry-run/apply split so a merge NEVER happens in the same tool-call that reviewed the diff:

### Step 1: summarise (default, no --apply)

```bash
run_stage4.py --run-id YYYY-MM-DD-slug [--profile NAME] [--dry-run]
```

Fetches `origin/fix/<run-id>`, computes a diff/artifact summary (diffstat vs merge base,
swarm-report highlights, checkpoint-log flags), prints it, and writes a **pending-merge marker**
to `~/harness/artifacts/<run-id>/pending-merge.json` keyed to the run-id, fix SHA, and a digest of
the diffstat. It NEVER merges.

### Step 2: apply the merge (--apply)

```bash
run_stage4.py --run-id YYYY-MM-DD-slug --apply [--profile NAME] [--dry-run]
```

Reads the pending-merge marker, **re-validates** it still matches the current branch state
(refuses if the fix SHA or diffstat changed between the two calls — a stale/mismatched marker from
an aborted run or a changed branch), merges `fix/<run-id>` into main with `--no-ff`, drops
`_harness/<run-id>/` from the merged tree, and pushes main to origin.

### Why two steps, not a prompt or a --confirm flag

An interactive prompt doesn't work in a non-TTY agent tool-call loop and is trivially satisfied via
piped stdin. A same-call `--confirm` flag is just as easy for an agent to self-supply as the action
it's meant to gate. The two-step split forces a hard boundary between 'summarise' and 'mutate' as
two separate tool invocations, produces a concrete artifact (the marker) that documents what was
reviewed and when, and gives Morley (or any wrapping orchestration/policy layer) a real point to
inspect before the second call happens.

**Important limitation:** the marker-file split reduces accidental/same-turn auto-merge risk and
creates an auditable review boundary, but it is NOT a complete authority boundary. An agent with
general shell access can call both steps in the same loop. True enforcement of human review
depends on controls outside this script (restricting which invocations are exposed to the agent,
or platform-level review gates). The `--apply` capability being present in the agent's own toolset
is an access-control/deployment decision for whoever wires up the agent's available tools — the
script cannot enforce that on its own.

### Inspection helper

```bash
run_stage4.py --run-id YYYY-MM-DD-slug --marker-only   # print marker path, exit
```

Prints the path to the pending-merge marker for a wrapping orchestration/policy layer to inspect.

## Repo profiles

All stages accept `--profile NAME` to target a repo defined in `profiles/<name>.json` (a workspace
separate from `~/harness/repo`). Omit `--profile` for the legacy NewChapter checkout. The profile
system was introduced in the `harness/repo-profiles` merge; see `profiles/archie.json` for an
example.
