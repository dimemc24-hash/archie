# Build: appraisal confidence surfaced at the human gates

You are building in a checkout of `dimemc24-hash/archie`, already on the correct
`build/<run-id>` branch. Do NOT switch branches, do NOT push.

## Sentinel protocol (mandatory)

At a decision checkpoint emit exactly `CHECKPOINT_REACHED:<id>` and STOP; await injected
guidance. When done AND all verification passes, emit exactly `BUILD_COMPLETE`.

One required checkpoint, BEFORE writing the code it affects:
1. `low-confidence-approve` — before implementing approve()'s behavior for non-high
   confidence.

## Read first

`_harness/<run-id>/kb/build-spec.md` (authoritative — implement it). Then the current
`antiques/approve.py`, `antiques/test_antiques.py`,
`dashboard-plugins/listings/dashboard/` (all three files), and
`skills/antiques-capture/SKILL.md` — match their patterns exactly.

## Hard constraints

- Touch ONLY: `antiques/approve.py`, `antiques/test_antiques.py`,
  `dashboard-plugins/listings/dashboard/*`, `skills/antiques-capture/SKILL.md`.
  Do NOT touch capture.py or any other sandbox script, the status machine,
  publish/pricing/common, or the marker digest semantics.
- Host-side code: Python stdlib only (fastapi allowed in plugin_api.py only).
- NO network calls in tests; injectable transports; zero-network house rule.
- Missing/malformed confidence NEVER crashes anything and NEVER passes as high —
  it is `unknown`. "I don't know" beats wrong.
- Match house style. No dead code, comments only where the code can't say it.

## Verification before BUILD_COMPLETE

`~/.hermes/hermes-agent/venv/bin/python3 -m pytest antiques/test_antiques.py -q` — all
green including new tests. If plugin_api.py changed, its existing test style must cover
the payload addition.
