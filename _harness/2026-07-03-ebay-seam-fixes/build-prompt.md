# Build: eBay seam fixes (publish path works unaided)

You are building in a checkout of `dimemc24-hash/archie`, already on the correct
`build/<run-id>` branch. Do NOT switch branches, do NOT push.

## Sentinel protocol (mandatory)

At a decision checkpoint emit exactly `CHECKPOINT_REACHED:<id>` and STOP; await injected
guidance. When done AND all verification passes, emit exactly `BUILD_COMPLETE`.

One required checkpoint, BEFORE writing the code it affects:
1. `category-strategy` — before implementing the leaf-category fix in publish.py.

## Read first

`_harness/<run-id>/kb/`: `build-spec.md` (authoritative — implement it),
`ebay-integration-notes.md` (exact live-API errors and the request shapes that WORKED —
ground truth for every fix and every test's fixture data).

Then read the current `antiques/publish.py`, `antiques/common.py`,
`antiques/test_antiques.py` before changing anything — match their patterns exactly
(injectable transports, `_HttpError`, test stub style).

## Hard constraints

- This is a FIX round on `antiques/publish.py`, `antiques/common.py`,
  `antiques/test_antiques.py`, `antiques/README.md` ONLY. Do not touch
  `skills/antiques-capture/` (sandbox skill), the status machine, `approve.py` marker
  semantics, or the dashboard plugin.
- Host-side code: Python stdlib only (fastapi allowed only in the plugin, which you are
  not touching).
- NO network calls in tests; every HTTP path goes through an injectable transport.
  Fixture bodies must mirror the REAL observed responses in ebay-integration-notes.md.
- Secrets: env names only in messages/logs; values never.
- `NotConnected` errors must keep naming exactly which env vars are missing.
- Match house style. No dead code, no TODO-spam, comments only where the code can't say it.

## Verification before BUILD_COMPLETE

`~/.hermes/hermes-agent/venv/bin/python3 -m pytest antiques/test_antiques.py -q` — all
green, including your new tests (one per fix minimum, asserting on the exact payload /
header / URL shapes from the kb notes).
