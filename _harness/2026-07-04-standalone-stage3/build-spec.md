# Build spec: standalone Stage-3 — generalize the swarm lane beyond NextChapter

## Context

The Stage-3 swarm lane (DO `transport.sh` → Hetzner `run_swarm.sh` → `peanut_wheel.py`
detect-only critics → `triage.py` work-order → `fix/<id>` back to origin) is hardwired to
NextChapter. On 2026-07-03 an archie-profile build chained into `transport.sh` and
aborted ("no build ref") because transport was looking in `~/harness/repo` — the
**NextChapter** checkout — for an **archie** branch. This build makes the lane
profile-driven so any harness repo profile (archie first) gets a swarm pass, while the
NextChapter flow keeps working **bit-for-bit identically**.

`kb/run_swarm.sh`, `kb/peanut_wheel.py`, `kb/triage.py` are the LIVE Hetzner scripts
(hand-managed there, not repo-tracked). `kb/stage3-lane-analysis.md` maps every
NextChapter-specific seam. `transport.sh` itself is in this repo — read it directly.

## Hard requirements

1. **Zero NextChapter regression.** Morley actively uses and hand-tunes the live lane
   (persona weights, `.bak-*` backups on the box). `transport.sh <id> [routes] [waves]`
   with no profile argument must behave exactly as today.
2. **The live Hetzner wheel files must not be clobbered.** How generalized swarm-side
   code reaches the box is checkpoint `swarm-script-deployment` — STOP and consult.
3. **Per-repo verification is configuration, not code.** How it's expressed is
   checkpoint `verify-lane-shape` — STOP and consult.

## Deliverables

### 1. `transport.sh` — profile-aware

`transport.sh <run-id> [routes] [waves] [--profile <name>]` (and/or `HARNESS_PROFILE`
env). With a profile: resolve the DO-side repo dir, the Hetzner target repo path
(e.g. `swarm/archie`), and the swarm invocation from the profile config. Without one:
today's NextChapter behavior, unchanged. Bootstrap: if the Hetzner target repo doesn't
exist, initialize it over SSH (`git init` + the baseline push transport already does)
so first use needs no manual setup. Keep every hardening behavior that exists today:
power-on check, freshest-ref selection, sentinel polling, gated origin push, abort
alerts, transport.log.

### 2. `profiles/archie.json` — swarm section

Extend the existing profile shape with the swarm config for archie: Hetzner repo path,
code-scope pattern (`\.py$` excluding tests/`_harness/`), no dev-server/routes (vision
lane skipped), deps step (none — stdlib), and the verification command
(`pytest antiques/test_antiques.py -q` must stay green; the shape of this config is
checkpoint `verify-lane-shape`).

### 3. Swarm-side generic runner — shipped from this repo

A generalized `run_swarm` (and whatever wheel/triage parameterization the deployment
checkpoint decides) that reads the per-repo config instead of the hardcoded
`$SWARM/newchapter`, `.ts|.tsx` scope, `npm ci`, and next-dev-server steps. Everything
repo-agnostic in the live scripts (sentinel contract, fix-branch mechanics,
`swarm-report.json`/`fix-log.json` handoff, detect-only + triage flow, hivemind-gate
visibility) is preserved — Stage 4 consumers (`notify_stage4.sh` greps
`swarm-report.json` on the fix branch) must keep working for both repos.

### 4. Docs

A short section in the repo docs (or README of the new swarm dir): the lane's contract
(what DO pushes, what Hetzner writes back, the sentinel), how to add the NEXT repo
profile, and the one-time-nothing bootstrap story.

## Verification (before BUILD_COMPLETE)

- `bash -n` on every touched/added shell script.
- Unit tests for the pure parts you can test host-side (profile resolution, config
  parsing, scope-pattern selection) in the house zero-network style.
- A `--dry-run` mode on the new transport path that prints the full plan (refs, target
  repo, swarm invocation) without any SSH — and a test asserting the plan for both the
  default (NextChapter) and archie profiles.
- DO NOT ssh to the Hetzner box during the build. DO NOT run a real transport. The
  first live archie swarm pass happens post-merge, driven by Morley/Archie.
