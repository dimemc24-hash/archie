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
| `swarm_config.py` | Config resolution: profiles + `.swarm.json` + fallback + schema versioning |
| `swarm-repos/` | Legacy config files (kept for drift/audit tooling; superseded by `.swarm.json`) |
| `swarm-repos/newchapter.swarm.json.template` | Reference template for the NextChapter `.swarm.json` |
| `swarm_drift.py` | Drift detector (repo vs live box) — pre-adoption audit tool |
| `README.md` | This file |

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
