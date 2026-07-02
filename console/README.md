# Archie Console

Web console for viewing and changing WHICH models the Fusion council runs
(panel seats, judge, synth — full and budget presets). FastAPI backend +
plain-JS frontend, deployed tailnet-only.

## Run locally

```bash
V=~/.hermes/hermes-agent/venv/bin/python3
$V console/app.py          # binds 127.0.0.1:9130
```

The first boot auto-generates `~/harness/console-token` (0600) if absent.
Read it to authenticate API calls:

```bash
cat ~/harness/console-token
```

The UI asks for the token once and stores it in localStorage.

## Deploy (tailnet-only)

```bash
# 1. Install the systemd user unit
cp console/hermes-console.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-console

# 2. Expose via Tailscale Serve (tailnet-only, no public domain needed)
tailscale serve --http=8090 127.0.0.1:9130
```

The console is now reachable at `http://<box>:8090` from any tailnet node.

## Auth posture (v1)

**Bearer token from a 0600 file** (`~/harness/console-token`), verified through
a single `verify_request(request)` seam in `console/app.py`.

- Auto-generated with `secrets.token_urlsafe(32)` on first boot, chmod 0600.
- All API routes except `/`, `/static/*`, `/api/health` require
  `Authorization: Bearer ***`.
- The token value is NEVER logged or printed by the app.
- File permissions are enforced: if `console-token` is group/world-readable,
  the app returns 500 until `chmod 0600` is applied.
- Network exposure control is the tailnet (same as the dashboard). The bearer
  token adds lightweight defense-in-depth on top of tailnet-only access.

### Auth upgrade path (OAuth)

The entire auth posture lives behind one function:

```python
def verify_request(request: Request) -> None:
    ...
```

To migrate to OAuth/OIDC when a public domain and provider app exist, swap the
body of `verify_request` to validate an OAuth bearer token (e.g. against an
OIDC JWKS endpoint) instead of reading the file token. No other route or
handler needs to change — they all call `verify_request(request)`.

### Known limitations (v1)

- **Token lifecycle:** no automated rotation or revocation. A compromised token
  requires manual file edit (`echo NEW_TOKEN > ~/harness/console-token`) and
  service restart. Future: add a rotation endpoint behind the existing seam.
- **Single shared token:** no per-user audit trail. All deploys are attributed
  to `"console"` in the audit log. Future: OAuth migration gives per-user identity.
- **Token distribution:** the token must be shared out-of-band with authorized
  operators (e.g. via a tailnet-encrypted channel). Do not put it in version control.
- **Backup risk:** `~/harness/console-token` is excluded by `.gitignore`
  (`*token*` pattern) but ensure backup tooling also excludes it.
- **No OAuth roadmap commitment:** the seam is minimal and documented but there
  is no scheduled migration checkpoint.

## Config schema

`~/harness/council-config.json` — all keys optional; missing key → hardcoded default:

```json
{
  "panel_full":   ["slug", "..."],
  "panel_budget": ["slug", "..."],
  "judge":        "slug",
  "synth_full":   "slug",
  "synth_budget": "slug",
  "updated_at":   "ISO-8601",
  "updated_by":   "console"
}
```

`fusion()` reads the file fresh on every call. Missing file → byte-for-byte
identical to the hardcoded constants. Corrupt JSON → fall back to ALL constants
+ one stderr warning (`[fusion] council-config invalid: …`).

## API

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | no | `{ok, config_file, catalog_cached}` |
| GET | `/api/config` | yes | Effective config: per-role value+source, defaults, path |
| GET | `/api/models?q=` | yes | OpenRouter catalog, filtered by substring |
| POST | `/api/config` | yes | Validate + deploy config (atomic write + audit) |
| POST | `/api/models/refresh` | yes | Drop catalog cache, refetch |

## Validation rules (POST /api/config)

- Every slug must exist in the live OpenRouter catalog → else 422.
- Panels: 2–5 seats, no duplicates → else 422.
- Judge/synth: non-empty strings → else 422.
- `anthropic/claude-opus*` → 422 (no-Opus July 2026 policy).
- Other `anthropic/*` → accepted, adds a warning.
- Slugs not in `port/harness_ledger.py` PRICE_TABLE → warning (ledger under-counts).

## Audit log

Deploys append a JSONL line to `~/harness/artifacts/console-audit.jsonl`:

```json
{"ts": "2026-07-02T...", "actor": "console", "before": {...}, "after": {...}}
```

## Tests

```bash
V=~/.hermes/hermes-agent/venv/bin/python3
$V -m pytest console/test_console.py -q
```

No network: `fetch_catalog` is stubbed via `app.state.fetch_catalog`.
