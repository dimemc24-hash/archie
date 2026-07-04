# Stage-3 lane analysis — every NextChapter-specific seam (2026-07-03)

Read alongside the live scripts in this kb. "DO" = the DigitalOcean driver box,
"Hetzner" = the swarm box (8 cores/16GB, `hetzner-swarm` SSH alias from DO).

## The contract (repo-agnostic, keep intact)

1. DO `transport.sh`: powers the box on (`hetzner_power.sh up`), pushes `gh-main`
   baseline + freshest `build/<id>` (origin-vs-local ancestry-checked) to a git repo on
   Hetzner, invokes `run_swarm.sh <build-branch> [routes] [waves]` over SSH.
2. Hetzner `run_swarm.sh`: checks out `build/<id>` as `fix/<id>`, derives changed-file
   scope from `gh-main..build/<id>`, runs `peanut_wheel.py` (DETECT-ONLY critics, waves
   with quiesce) → `swarm-findings.json`, then `triage.py` (budget-tier triage brain) →
   `swarm-workorder.json` batches; commits `_harness/<id>/{swarm-report.json,fix-log.json}`
   onto `fix/<id>`; writes sentinel `~/swarm/status/<id>.done` (JSON with rc/status) LAST,
   on ANY exit.
3. DO transport polls the sentinel (survives dropped SSH), fetches `fix/<id>` back,
   gates the origin push on the real outcome, alerts Morley on abort.
4. `notify_stage4.sh` (DO cron) pings Morley only for fix branches carrying
   `_harness/<id>/swarm-report.json`.

## NextChapter hardwiring, seam by seam

**transport.sh (in THIS repo):**
- `REPO="$HARNESS/repo"` — the NextChapter checkout on DO. (The archie workspace is
  `~/harness/workspaces/archie/repo`, resolved by `profiles/archie.json` today for
  stage2; transport predates profiles.)
- `git remote add hetzner hetzner-swarm:swarm/newchapter` — hardcoded target repo.

**run_swarm.sh (live on Hetzner, hand-managed, in this kb):**
- `REPO="$SWARM/newchapter"`.
- Code scope: `grep -E '\.(ts|tsx)$'` excluding tests/`_harness/`/`.d.ts`.
- Deps: `npm ci --legacy-peer-deps` when `package-lock.json` changed.
- Vision lane: `npm run dev` on :3000, 120s wait, DEGRADED + drop routes if it
  never comes up. (For a Python/API repo: no dev server, no routes — skip lane.)
- `.git/info/exclude` protection for swarm-local files (`swarm_capture.mjs`,
  `.env.local`) against the wheel's `git clean`.

**peanut_wheel.py (live on Hetzner, in this kb):**
- Critics are `hermes -z <brief> -m <model> --provider openrouter --yolo` subprocesses —
  repo-agnostic (they read "this repo" = cwd).
- `max_workers=8` executor (recently raised from 4 — keep 8).
- Vision personas take route screenshots (skip when no routes).
- Verify/baseline machinery references `tsc` baselines (`tsc.baseline` files on the box)
  — the per-repo verify equivalent for archie is pytest-green.

**triage.py (live on Hetzner, in this kb):** operates on the findings JSON — check for
path assumptions but it is substantially repo-agnostic.

## Facts that matter for design

- The Hetzner scripts are NOT in any git repo. Morley hand-tunes them (persona weights,
  thresholds) and keeps `.bak-*` copies beside them (see the box: `peanut_wheel.py.bak.*`).
  Any deployment scheme that overwrites his live NextChapter files loses his tuning —
  that's why `swarm-script-deployment` is a checkpoint.
- The 2026-07-03 abort: an archie build chained `transport.sh` → it looked for
  `build/2026-07-03-ebay-seam-fixes` in the NextChapter repo → correct abort, wrong lane.
- Hetzner has hermes CLI + OpenRouter env + venv python3. The archie repo needs a
  checkout there (does not exist yet — bootstrap is part of this build's transport work).
- Archie repo verification: `~/.hermes/hermes-agent/venv/bin/python3 -m pytest
  antiques/test_antiques.py -q` on DO; on Hetzner plain `python3 -m pytest` (check what
  the box has — pytest may need the repo's needs; keep verify configurable, that's the
  checkpoint).
- The archie repo also contains the HARNESS driver code at its root (stage2_build.py,
  transport.sh...). A swarm pass over an archie build should scope to the changed .py
  files like any other — nothing special.
