# Swarm lane — generic profile-driven Stage-3

This directory contains the **generic, repo-tracked** swarm runner that extends
the Stage-3 swarm lane beyond NextChapter to any harness repo profile.

## Deployment model: side-by-side (option c)

The live Hetzner swarm scripts (`run_swarm.sh`, `peanut_wheel.py`, `triage.py` in
`~/swarm/`) are hand-managed by Morley and are **NOT in any git repo**. They
contain live tuning (persona weights, thresholds) and `.bak-*` backups on the
box. This generic runner ships **side-by-side** — new files in this repo, rsynced
to `~/swarm/generic/` on each profile transport, leaving the live NextChapter
trio completely untouched.

**Adoption gate:** the generic runner must survive at least two successive
Archie-profile QA sweeps without regressions before a permanent adoption (option
b: bring the live scripts into the repo as the single source of truth) is
considered. Until then, NextChapter uses the live runner; archie uses the generic
runner.

**Drift detection:** `swarm_drift.py` compares repo-tracked scripts against the
live box, making the future cutover auditable instead of a guessing game.

## Per-repo configuration: `.swarm.json` (decision: verify-lane-shape option c)

Each target repo carries a **self-describing `.swarm.json`** at its root. The
config travels with the code that reads it, so any repo becomes swarm-able by
adding one file, and there is no cross-box configuration drift.

The generic runner reads `.swarm.json` from the checked-out working tree after
`git checkout`. No config rsync is needed — the file is already in the repo.

### Schema

```json
{
  "version": 1,
  "name": "archie",
  "description": "optional human-readable description",
  "hetzner_repo_path": "swarm/archie",
  "scope_pattern": "\\.(py)$",
  "exclude_pattern": "(__pycache__/|\\.pyc$|/test_)",
  "lockfile": "",
  "deps_step": "none",
  "dev_server_cmd": "",
  "dev_server_health": "",
  "verify_cmd": "python3 -m pytest antiques/test_antiques.py -q",
  "routes": ""
}
```

| Field | Purpose |
|-------|---------|
| `version` | Schema version (currently 1). See Schema Evolution below. |
| `name` | Repo identifier (matches the profile's `swarm.repo_name`) |
| `hetzner_repo_path` | Where the repo lives on the Hetzner box (e.g. `swarm/archie`) |
| `scope_pattern` | Regex for in-scope code files (e.g. `\.(ts\|tsx)$` or `\.(py)$`) |
| `exclude_pattern` | Regex for files to exclude from scope (tests, generated, etc.) |
| `lockfile` | File whose change triggers the deps step (e.g. `package-lock.json`) |
| `deps_step` | Shell command for dependency install, or `"none"` |
| `dev_server_cmd` | Command to start the dev server (vision lane), or `""` to skip |
| `dev_server_health` | Health-check URL for the dev server (default: `http://localhost:3000`) |
| `verify_cmd` | Command to verify the build (e.g. `pytest`, `tsc --noEmit`) |
| `routes` | CSV of routes for the vision lane, or `""` |

### Fallback when `.swarm.json` is absent

When a profile is used but the target repo has no `.swarm.json`, the resolver
falls back to hardcoded NextChapter-shaped defaults (scope=`\.ts/.tsx`,
deps=`npm ci`, verify=`tsc --noEmit`) AND **emits a clear warning** to stderr.

**BLINDSPOT ADDRESSED — silent fallback danger:** a missing `.swarm.json` must
never mask a missing verify step or wrong deps and produce a false-green
pipeline. The warning is always emitted on fallback — in `swarm_config.py`
resolution, in `transport.sh --dry-run` output, and in `run_swarm_generic.sh`
logs — so the omission is visible at every layer.

### Schema evolution

**BLINDSPOT ADDRESSED — schema evolution risk:** the `.swarm.json` schema is
versioned via the `version` field. The runner accepts:

- **Equal version** (e.g. `version: 1` with `SWARM_CONFIG_VERSION = 1`): accepted silently.
- **Older version** (e.g. `version: 0`): accepted with a `DeprecationWarning`.
- **Newer version** (e.g. `version: 99`): **rejected** with a `ValueError` to
  prevent silent misinterpretation of unknown fields.

This ensures backward compatibility indefinitely — old configs keep working —
while preventing a breaking parser change from silently misinterpreting a
config written for a future version.

### Global change overhead

**BLINDSPOT ACKNOWLEDGED — global change overhead:** updating a rule across all
repos (e.g., a new default Node version) requires a PR to every repository
instead of one central file. This is the trade-off of config-travels-with-code:
each repo is self-contained, but cross-repo changes are N PRs, not 1. Mitigation:
the `FALLBACK_*` constants in `swarm_config.py` provide a single place to update
the default shape, and a future automation can bump `.swarm.json` versions
across repos when a new schema adds fields.

## The lane contract (unchanged from the live runner)

```
DO (transport.sh)                         Hetzner (run_swarm_generic.sh)
─────────────────                         ──────────────────────────────
1. power-on check (hetzner_power.sh)  →
2. push gh-main baseline            →   git repo at ~/swarm/<repo-name>
3. push build/<id> (freshest ref)   →   refs/heads/build/<id>
4. rsync generic runner             →   ~/swarm/generic/ (runner only; .swarm.json travels with checkout)
5. ssh: run_swarm_generic.sh <repo> <build> [routes] [waves]
                                         a. checkout build/<id> as fix/<id>
                                         b. read .swarm.json from repo root (fallback + warning if absent)
                                         c. derive changed-file scope (per-repo pattern)
                                         d. deps step (per-repo config)
                                         e. dev server / vision lane (per-repo config)
                                         f. peanut_wheel.py (DETECT-ONLY) → swarm-findings.json
                                         g. triage.py → swarm-workorder.json
                                         h. commit _harness/<id>/{swarm-report.json,fix-log.json}
                                         i. write sentinel ~/swarm/status/<id>.done (JSON) LAST
6. poll sentinel (survives dropped SSH)
7. fetch fix/<id> from hetzner      ←
8. push fix/<id> to origin (Stage 4)
```

**Sentinel contract:** `~/swarm/status/<id>.done` is a JSON file
`{"id":"...","rc":0,"status":"success","fix_sha":"...","fixes":N}` written on
ANY exit (even crash). Transport polls it and gates the origin push on the real
outcome (no fake-green).

**Stage-4 consumers:** `notify_stage4.sh` greps `_harness/<id>/swarm-report.json`
on the fix branch. The JSON shape is identical for both repos — it includes a
`repo` field so Stage 4 can distinguish repos if needed.

## Files

| File | Purpose |
|------|---------|
| `run_swarm_generic.sh` | Generic swarm runner (profile-driven, repo-tracked) |
| `generic_triage.py` | Repo-tracked triage wrapper — redirects live triage.py's hardcoded paths to the correct repo (zero NextChapter regression) |
| `swarm_config.py` | Config resolution: profiles + `.swarm.json` + fallback + schema versioning + cwd/path derivation + scope guard |
| `swarm-repos/` | Legacy config files (kept for drift/audit tooling; superseded by `.swarm.json`) |
| `swarm-repos/newchapter.swarm.json.template` | Reference template for the NextChapter `.swarm.json` |
| `swarm_drift.py` | Drift detector (repo vs live box) — pre-adoption audit tool |
| `README.md` | This file |

## CWD contract and the empty-scope sanity guard

**BLINDSPOT ADDRESSED — wrong-repo false-green (operator-console build,
2026-07-04):** the first real generic-runner run "succeeded" (sentinel rc=0)
but produced garbage: critics reviewed the **NextChapter** repo instead of
**archie**, and triage crashed looking for findings in the wrong place. Two
root causes, both fixed in `run_swarm_generic.sh`:

### 1. CWD must be bound to the target repo for the wheel AND triage

The generic runner does `cd "$REPO"` at startup, but that cwd was not
guaranteed when `peanut_wheel.py` spawned its critic subprocesses — the
critics read in-scope files (relative paths like `antiques/approve.py`) from
whatever cwd they inherited, which resolved to the NextChapter checkout. The
runner now wraps BOTH the wheel and triage invocations in an explicit
`( cd "$REPO" && ... )` subshell so the cwd is always the target repo
checkout, regardless of what intervening subshells (e.g. the deps step's
`bash -lc`) did to the parent shell's cwd.

The cwd derivation is centralized in `swarm_config.resolve_repo_cwd()` so it
is unit-testable host-side (no SSH, no network).

### 2. Empty-scope sanity guard

Before running the wheel, the runner verifies that the in-scope files
(computed from the git diff) actually exist under `$REPO`. If the scope is
non-empty but **zero** in-scope files are found on disk, the runner **FAILS
LOUD** (non-zero exit, clear log message) rather than letting critics review
an unrelated tree. This catches the exact failure mode that produced the
operator-console false-green: the scope was correct on paper but the files
didn't exist relative to the runner's cwd.

The guard logic is centralized in `swarm_config.scope_files_exist()` (unit-
testable). The shell runner also has an inline guard so it can abort before
the wheel process starts.

### 3. Triage path redirection (generic_triage.py)

The live `~/swarm/triage.py` hardcodes `~/swarm/newchapter` as the repo root.
For a non-NextChapter repo it would look for `swarm-findings.json` in the
wrong place and crash (as it did in the operator-console run). The
repo-tracked `swarm/generic_triage.py` wrapper is shipped alongside the
runner to `~/swarm/generic/` and is invoked as:

```
generic_triage.py <run_id> <repo_root> [live_triage_path]
```

It runs the live `triage.py` with `HOME` overridden to a temp dir where
`swarm/newchapter` is a symlink to `<repo_root>`, so the live script's
hardcoded `~/swarm/newchapter/...` paths resolve to the correct repo —
without editing the live script. **Zero NextChapter regression:** the live
`run_swarm.sh` path never calls this wrapper; it calls `triage.py` directly,
and when `<repo_root>` IS `~/swarm/newchapter`, the symlink target is the
NextChapter checkout (identity).

The triage exit code is now **gated**: if triage crashes, the runner FAILS
instead of reporting success with zero batches. In the operator-console run,
the `FileNotFoundError` from triage was swallowed by `tee` and the runner
reported success.

## How to add the NEXT repo profile

1. Create `profiles/<name>.json` on the DO side (workspace, repo_url).
2. Add a `"swarm"` section to `profiles/<name>.json` (see `profiles/archie.json`):
   ```json
   "swarm": {
     "repo_name": "<name>",
     "hetzner_repo_path": "swarm/<name>",
     "runner": "generic",
     "bootstrap": true
   }
   ```
3. Commit a `.swarm.json` at the **root of the target repo** with the repo's
   scope/deps/verify config (use `swarm-repos/newchapter.swarm.json.template`
   as a reference for the shape).
4. Run `transport.sh <run-id> --profile <name>` — the bootstrap logic will
   `git init` the target repo on Hetzner on first use if it doesn't exist.

## Bootstrap (first-time-nothing)

When `transport.sh --profile <name>` runs and the Hetzner target repo doesn't
exist (`~/swarm/<name>` is missing), it initializes it over SSH:

```
ssh hetzner-swarm "mkdir -p ~/swarm/<name> && cd ~/swarm/<name> && git init"
```

Then the baseline push (`gh-main`) and build push proceed as normal. No manual
setup is needed — first use is fully automated.

## Local config (untracked, for live tuning)

BLINDSPOT ADDRESSED: "Treating the scripts as monoliths misses the opportunity
to externalize persona weights and thresholds into a local, untracked config
file."

For **live tuning** that should not enter git (persona weights, model selection,
thresholds), a `.swarm-local.json` file can be placed on the box at
`~/swarm/generic/.swarm-local.json`. This file is gitignored and read-merged at
runtime by the generic runner (future hook; the infrastructure is in place).
This allows adoption today while keeping live tuning hand-managed on the box.
