# Build: Stage-3 lane fix (generic runner must operate on its own repo)

You are building in a checkout of `dimemc24-hash/archie`, already on the correct
`build/<run-id>` branch. Do NOT switch branches, do NOT push.

## Sentinel protocol (mandatory)

At a decision checkpoint emit exactly `CHECKPOINT_REACHED:<id>` and STOP; await injected
guidance. When done AND all verification passes, emit exactly `BUILD_COMPLETE`.

No checkpoints in this build — the two root causes are diagnosed and the fix approach is
constrained by the spec. Implement directly. If you believe a real design fork exists that
the spec did not anticipate, emit `CHECKPOINT_REACHED:unplanned-<slug>` and STOP rather
than guessing.

## Read first

`_harness/<run-id>/kb/`: `build-spec.md` (authoritative), `run-log.txt` (the actual failed
run — your ground truth for the two bugs), `failed-findings.json` (what it wrongly
produced — note the NextChapter file paths). Then read, in the repo:
`swarm/run_swarm_generic.sh`, `swarm/swarm_config.py`, `swarm/README.md`,
`swarm/swarm-repos/*.json`, `.swarm.json`, and `transport.sh` — understand the PR #13
side-by-side deployment model before changing anything.

## Hard constraints

- Zero NextChapter regression: the live `run_swarm.sh` path is untouched; any triage
  change is backward-compatible (defaults to NextChapter when the new arg is absent).
- The fix ships from THIS repo (`swarm/run_swarm_generic.sh` + generic triage
  handling). You cannot reach the Hetzner box — do not try to ssh or edit `~/swarm/*`.
- A run that reviews the wrong repo must FAIL LOUD, never report success. Add the
  empty-scope sanity guard.
- Shell house style (`set -uo pipefail`, PATH self-fix, abort-with-alert, logged).
  Python parts stdlib-only, zero-network tests.
- Touch only `swarm/` and its tests.

## Verification before BUILD_COMPLETE

- `bash -n` on every touched shell script.
- `~/.hermes/hermes-agent/venv/bin/python3 -m pytest test_swarm_config.py antiques/test_antiques.py -q`
  green, plus your new tests for the cwd/path derivation and the empty-scope guard.
- `bash transport.sh 2026-07-05-stage3-lane-fix --profile archie --dry-run` prints a
  correct plan (no SSH).
