# Build: antiques listing pipeline body (provider seams, no live eBay)

You are building in a checkout of `dimemc24-hash/archie`, already on the correct
`build/<run-id>` branch. Do NOT switch branches, do NOT push.

## Sentinel protocol (mandatory)

At a decision checkpoint emit exactly `CHECKPOINT_REACHED:<id>` and STOP; await injected
guidance. When done AND all verification passes, emit exactly `BUILD_COMPLETE`.

Two required checkpoints, both BEFORE writing the code they affect:
1. `image-persistence` — before writing capture.py / common.py photo handling.
2. `capture-boundary` — before finalizing what capture.py does vs a host worker.

## Read first

`_harness/<run-id>/kb/`: the build spec (authoritative — implement it),
`ebay-cheatsheet.md` (exact API shapes for the provider seams),
`appraisal-SKILL.md` (the upstream skill whose output you're capturing),
`queue_spec.py` (the sandbox-bridge precedent), `plugin_api.py` + `index.js` +
`manifest.json` (dashboard plugin house style), `policy-note.md` (env names, sandbox
constraints, insulation rules).

## Hard constraints

- `skills/antiques-capture/scripts/capture.py`: **Python stdlib ONLY** (urllib, json,
  ssl, pathlib, argparse). It runs inside a Docker sandbox with nothing pip-installed.
  It may see env: `ARCHIE_SUPABASE_URL`, `ARCHIE_SUPABASE_SERVICE_KEY` — and NOTHING
  else. Never print the key. Never read host paths.
- `antiques/*.py` runs on the HOST (hermes venv: fastapi/pytest available), but keep
  third-party imports to fastapi (plugin only) — everything else stdlib.
- NO network calls anywhere in tests; every HTTP path goes through an injectable
  transport function.
- Secrets: env names only in messages/logs; values never.
- eBay slugs/env expected later: `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_REFRESH_TOKEN`,
  `EBAY_OAUTH_TOKEN`, `EBAY_FULFILLMENT_POLICY_ID`, `EBAY_PAYMENT_POLICY_ID`,
  `EBAY_RETURN_POLICY_ID`, `EBAY_MERCHANT_LOCATION_KEY`. `NotConnected` errors must name
  exactly which are missing.
- Match house style (see kb). No dead code, no TODO-spam, comments only where the code
  can't say it.

## Status machine (single source of truth in common.py)

```
draft → priced → approved → listed → sold → shipped
draft → approved            (manual price at approve time)
any   → rejected | error    (terminal; error records why in notes)
```
`advance()` refuses anything else and is the ONLY writer of `status`.

## Deliverables

Exactly as the spec lists: `antiques/{migration/001_listings.sql, common.py, pricing.py,
approve.py, publish.py, fulfill.py, test_antiques.py, README.md}`,
`skills/antiques-capture/{SKILL.md, scripts/capture.py}`,
`dashboard-plugins/listings/dashboard/{manifest.json, dist/index.js, plugin_api.py}`.

README.md documents: the flow end-to-end, the migration paste-into-Studio step, cron
lines to install LATER (pricing worker optional, fulfill poller — commented, not
installed), the eBay connect checklist (which env vars, where they come from), and the
approval CLI examples.

## Verification (ALL must pass before BUILD_COMPLETE)

```
V=~/.hermes/hermes-agent/venv/bin/python3
$V -m py_compile antiques/*.py skills/antiques-capture/scripts/capture.py dashboard-plugins/listings/dashboard/plugin_api.py
$V -m pytest antiques/test_antiques.py -q
python3 port/test_port.py                                   # stays 100/100
python3 skills/antiques-capture/scripts/capture.py --help    # plain python3, stdlib only
python3 - <<'EOF'                                            # capture.py is stdlib-only, enforced
import ast, sys
tree = ast.parse(open('skills/antiques-capture/scripts/capture.py').read())
STDLIB_OK = {'urllib','urllib.request','urllib.error','json','ssl','pathlib','argparse','os','sys','mimetypes','uuid','datetime','base64','hashlib','time','typing'}
bad = []
for n in ast.walk(tree):
    if isinstance(n, ast.Import): bad += [a.name for a in n.names if a.name.split('.')[0] not in {s.split('.')[0] for s in STDLIB_OK}]
    if isinstance(n, ast.ImportFrom) and n.module: 
        if n.module.split('.')[0] not in {s.split('.')[0] for s in STDLIB_OK}: bad.append(n.module)
print('BAD-IMPORTS:' + ','.join(bad) if bad else 'stdlib-only-ok')
assert not bad
EOF
python3 -c "import json; json.load(open('dashboard-plugins/listings/dashboard/manifest.json')); print('manifest-ok')"
```

When everything is green, emit `BUILD_COMPLETE`.
