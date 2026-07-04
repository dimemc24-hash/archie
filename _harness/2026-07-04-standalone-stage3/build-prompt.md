# Build: standalone Stage-3 (profile-driven swarm lane)

You are building in a checkout of `dimemc24-hash/archie`, already on the correct
`build/<run-id>` branch. Do NOT switch branches, do NOT push.

## Sentinel protocol (mandatory)

At a decision checkpoint emit exactly `CHECKPOINT_REACHED:<id>` and STOP; await injected
guidance. When done AND all verification passes, emit exactly `BUILD_COMPLETE`.

Two required checkpoints, both BEFORE writing the code they affect:
1. `swarm-script-deployment` — before creating the swarm-side scripts / touching how
   they reach the Hetzner box.
2. `verify-lane-shape` — before writing the per-repo verification/scope config.

## Read first

`_harness/<run-id>/kb/`: `build-spec.md` (authoritative), `stage3-lane-analysis.md`
(seam map — ground truth), `run_swarm.sh` + `peanut_wheel.py` + `triage.py` (the LIVE
Hetzner scripts — the only copies in reach; they are NOT elsewhere in this repo).
Then read `transport.sh`, `profiles/archie.json`, `hetzner_power.sh`,
`notify_stage4.sh` in the repo root.

## Hard constraints

- **Zero NextChapter regression**: `transport.sh <id> [routes] [waves]` with no profile
  must behave byte-for-byte as today (same refs, same remote, same messages Morley's
  tooling greps). Additive changes only on that path.
- Do NOT modify the live Hetzner box from this build: no ssh to `hetzner-swarm`, no
  real transport runs. Bootstrap logic ships as code that runs at first REAL transport.
- Shell house style: `set -uo pipefail`, PATH self-fix, abort-with-alert pattern,
  everything logged to the run's transport.log — match transport.sh as it stands.
- Python parts: stdlib only, injectable seams, zero-network tests.
- Do not touch: antiques/, skills/, dashboard-plugins/, stage2_build.py, fusion.py,
  notify_stage4.sh (it must keep working unchanged — verify by reading, not editing).

## Verification before BUILD_COMPLETE

- `bash -n` every touched shell script.
- `~/.hermes/hermes-agent/venv/bin/python3 -m pytest antiques/test_antiques.py -q`
  still green (you didn't touch it — prove it).
- New unit tests green (profile/config resolution, dry-run plan for BOTH the default
  and archie profiles).
- `transport.sh --dry-run` (new) prints the correct plan for both profiles.
