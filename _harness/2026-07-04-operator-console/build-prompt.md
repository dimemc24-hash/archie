# Build: Antiques Operator Console (write-enabled Listings tab)

You are building in a checkout of `dimemc24-hash/archie`, already on the correct
`build/<run-id>` branch. Do NOT switch branches, do NOT push.

## Sentinel protocol (mandatory)

At a decision checkpoint emit exactly `CHECKPOINT_REACHED:<id>` and STOP; await injected
guidance. When done AND all verification passes, emit exactly `BUILD_COMPLETE`.

Two required checkpoints, both BEFORE writing the code they affect:
1. `publish-confirm-boundary` — before the publish endpoints/UI.
2. `low-confidence-ux` — before the approve form's ack control.

## Read first

`_harness/<run-id>/kb/`: `build-spec.md` (authoritative), `ops-notes.md` (health-strip
data sources, ack contract, current TEST rows). Then the code you are extending:
`dashboard-plugins/listings/dashboard/{plugin_api.py, dist/index.js, manifest.json}`,
`antiques/{approve.py, publish.py, pricing.py}` (the ONLY writers — your endpoints wrap
them), `antiques/test_antiques.py` (house test style), and how the dashboard authorizes
plugin API calls today (match it exactly).

## Hard constraints

- Touch ONLY `dashboard-plugins/listings/dashboard/*` and its tests. antiques/ modules
  are READ-ONLY dependencies — wrap, never modify, never duplicate their status/marker
  logic.
- plugin_api.py: python stdlib + fastapi only. dist/index.js: hand-authored IIFE
  matching the existing style — no build step, no new deps, no framework additions.
- NO new auth mechanism. Reuse the dashboard's existing plugin-API auth exactly.
- Structured errors: LowConfidenceError → 409 with the confidence payload;
  IllegalTransition/stale marker → 409 with the reason; never a bare 500 for a
  domain condition.
- Secrets: env NAMES only in health output; never values; never the refresh token/log
  contents beyond the timestamp.
- Zero network in tests (stub client + injectable seams, as the existing tests do).

## Verification before BUILD_COMPLETE

`~/.hermes/hermes-agent/venv/bin/python3 -m pytest antiques/test_antiques.py test_swarm_config.py -q`
all green, plus your new plugin tests. `node --check` on dist/index.js.
