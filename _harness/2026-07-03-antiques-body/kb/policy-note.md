# Environment, security, and policy notes (read me first)

- **Sandbox reality:** the capture skill runs in the Docker `default` profile:
  `nikolaik/python-nodejs` image, NO pip installs, NO host filesystem, NO harness. Only
  these secrets are forwarded: `ARCHIE_SUPABASE_URL`, `ARCHIE_SUPABASE_SERVICE_KEY`
  (plus Twilio/Graph/ElevenLabs, irrelevant here). Everything else in the pipeline runs
  on the HOST (hermes venv at `~/.hermes/hermes-agent/venv`).
- **Insulation rules:** Archie's Supabase only (`ARCHIE_SUPABASE_*`). Never reference the
  firm's Supabase. Secret VALUES never in logs, errors, or test fixtures — env NAMES only.
- **Human gate:** nothing irreversible without Morley. `approved → listed` requires the
  two-step marker (approve writes it, publish --apply re-validates it — same semantics as
  run_stage4.py, which is in this repo if you want the reference). Labels/fulfillment run
  only against orders that already exist (a sale is the trigger, not a decision).
- **Model policy (July 2026):** no Opus anywhere; avoid Anthropic models for labor. This
  build's code never picks models itself — but README examples must not name Anthropic
  models.
- **House patterns in this repo to mirror:** `skills/harness-control/` (SKILL.md shape,
  thin wrappers, do-lock discipline — note: this pipeline does NOT take the do-lock; it
  never mutates harness state), `dashboard-plugins/*/dashboard/` (manifest + IIFE +
  plugin_api.py), `console/` (auth seam + atomic writes + audit lines, if you need a
  reference for atomic file writes).
- **Supabase REST idioms:** PostgREST base `<URL>/rest/v1/<table>`, headers
  `apikey: <service key>` + `Authorization: Bearer <service key>` +
  `Prefer: return=representation`. Storage: `<URL>/storage/v1/object/<bucket>/<path>`
  (POST upload), bucket create `POST <URL>/storage/v1/bucket`, signed URL
  `POST <URL>/storage/v1/object/sign/<bucket>/<path>` with `{"expiresIn": seconds}`.
- **Alerting:** `~/harness/alert.sh "<msg>"` Telegram-pings Morley (host side only).
- **Dashboard plugin API mount:** `/api/plugins/listings/…`; UI reads
  `window.__HERMES_PLUGIN_SDK__` exactly like the kb examples.
