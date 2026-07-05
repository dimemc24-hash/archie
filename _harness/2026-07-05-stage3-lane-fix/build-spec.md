# Build spec: Stage-3 lane fix — the generic runner must operate on ITS OWN repo

## Context

The standalone Stage-3 lane (merged PR #13) got its first real run on the archie repo
(operator-console build, 2026-07-04). Two seams failed — the run "succeeded" (sentinel
rc=0) but produced garbage: the critics reviewed the wrong repo and triage crashed.
Ground truth is `kb/run-log.txt` (the actual generic-runner log from the failed run) and
`kb/failed-findings.json` (what it wrongly produced).

**Observed failure:**
- The generic runner computed the CORRECT archie scope
  (`code scope: antiques/approve.py,dashboard-plugins/listings/dashboard/plugin_api.py`)
  and wrote findings to the correct path (`~/swarm/archie/_harness/.../swarm-findings.json`).
- BUT the critics returned 7 findings about **NextChapter files**
  (`app/(main)/cases/[caseId]/...`) that do not exist in the archie repo. The critic
  subprocesses (`peanut_wheel.py`, invoked as `/home/hermes/swarm/peanut_wheel.py`) read
  files from their CURRENT WORKING DIRECTORY, which was NOT the archie checkout — so they
  reviewed whatever repo the wheel process was cwd'd into (the NextChapter checkout).
- THEN `triage.py` crashed:
  `FileNotFoundError: '/home/hermes/swarm/newchapter/_harness/2026-07-04-operator-console/swarm-findings.json'`
  — triage.py has `~/swarm/newchapter` hardcoded and looked there, though the findings
  were in `~/swarm/archie/...`.

## Root causes (both in the swarm-side generic runner, `swarm/run_swarm_generic.sh`, and triage)

1. **Working directory not bound to the target repo.** The generic runner passes the
   correct `--code` scope and `--findings-out` path to `peanut_wheel.py`, but the wheel
   and its critic subprocesses run with a cwd that is not the target repo's checkout
   (`$HETZNER_REPO_PATH`, e.g. `~/swarm/archie`). Whatever mechanism the runner uses to
   invoke the wheel/triage must guarantee cwd == the target repo root, so "read the
   in-scope files in this repo" resolves against the RIGHT repo. Verify against the live
   `run_swarm.sh` (NextChapter) which cd's into `$REPO` first — the generic runner must do
   the equivalent for its configured repo path.

2. **`triage.py` hardcodes `~/swarm/newchapter`.** It must take the target repo root (or
   the findings path / repo path) as a parameter from the generic runner, not assume
   NextChapter. Fix triage.py to accept the repo root explicitly and the generic runner to
   pass it. (triage.py is a LIVE Hetzner script, hand-managed — see the deployment
   constraint below.)

## Hard requirements

- **Zero NextChapter regression.** The live `run_swarm.sh` + `peanut_wheel.py` + the
  CURRENT `triage.py` invocation for NextChapter must keep working exactly as before. If
  triage.py must change, the change must be backward-compatible: default to NextChapter
  behavior when the new repo-root argument is absent, OR the generic runner ships its own
  triage invocation — follow whatever the PR #13 deployment decision (`side-by-side`,
  generic scripts shipped from the repo to `~/swarm/generic/`) established. Read
  `swarm/run_swarm_generic.sh` and `swarm/README.md` in THIS repo to see that decision.
- **The fix lives in this repo's shipped generic runner** (`swarm/run_swarm_generic.sh`
  and, if triage needs parameterizing, a repo-tracked generic triage or a documented patch
  to the shipped one). Do NOT assume you can edit the live `~/swarm/*.py` files from the
  build — you cannot reach the Hetzner box. Ship the corrected generic runner from the
  repo; transport rsyncs it on the next run (that is the existing mechanism).
- Add a guard: if the computed scope is non-empty but ZERO in-scope files exist under the
  runner's cwd, the runner must FAIL LOUD (non-zero, clear message) rather than let critics
  review an unrelated tree. A run that reviews the wrong repo must never report success.

## Deliverables

- `swarm/run_swarm_generic.sh` — cwd bound to the resolved target-repo checkout for BOTH
  the wheel and triage invocations; the empty-scope-sanity guard; pass the repo root to
  triage.
- Generic triage handling (repo-tracked wrapper or parameterization) so triage reads the
  correct `_harness/<id>/swarm-findings.json`. Keep NextChapter's path working.
- `swarm/README.md` — document the cwd contract and the sanity guard.
- Tests: extend `test_swarm_config.py` (or a sibling) to cover the new
  resolution/guard logic that is unit-testable host-side (the cwd/path derivation, the
  empty-scope guard decision). Zero network; no SSH.

## Acceptance

- Existing suites stay green (antiques 113 + swarm-config).
- A unit test proves: given a target repo config, the runner derives the correct cwd and
  triage findings-path for that repo (archie AND newchapter cases), and the empty-scope
  guard trips when no in-scope file exists in the cwd.
- `bash -n` clean on all shell. No changes outside `swarm/` except tests.
- Dry-run (`transport.sh ... --profile archie --dry-run`) still prints a correct plan.
