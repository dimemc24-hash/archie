# Build spec: Antiques Operator Console — the Listings tab becomes the workstation

## Context

The pipeline is fully connected (eBay sandbox publishes work unaided; appraisal
confidence gates approve). But every operator decision is still a CLI one-liner:
approve/ack, reject, price override, publish. This build makes the dashboard Listings
tab the daily tool: photograph in Telegram → review/decide/publish in the dashboard.
Morley is the sole operator; the dashboard is Tailscale-gated with existing auth.

## Deliverables

### 1. `dashboard-plugins/listings/dashboard/plugin_api.py` — mutation endpoints

POST endpoints that are thin wrappers over the EXISTING single-writer functions —
`antiques.approve.approve/reject`, `antiques.pricing.price_listing`,
`antiques.publish.publish_listing` — never reimplementing their logic or the marker
semantics:

- `POST /listings/{id}/approve` — weight_oz, dims, optional price_override, and the
  low-confidence acknowledgment per checkpoint `low-confidence-ux` (see below).
  Surfaces `LowConfidenceError` as a structured 409 the UI can render, never a 500.
- `POST /listings/{id}/reject` — reason (required).
- `POST /listings/{id}/publish/prepare` — dry-run via DryRunProvider: returns exactly
  what would be sent (inventory item + offer payloads) for human review.
- `POST /listings/{id}/publish/apply` — gated per checkpoint `publish-confirm-boundary`.
- `GET /health` — pipeline health strip data: EBAY_* env presence (names only, never
  values), age of the last OK line in `~/.hermes/ebay-refresh.log`, OpenRouter credits
  via `port/openrouter_credits.get_credits()` (graceful if unavailable).

Auth: match the dashboard's existing auth pattern for plugin APIs exactly — the queue
endpoint already returns 401 unauthenticated. NO new auth surface, no new tokens.

### 2. `dashboard-plugins/listings/dashboard/dist/index.js` — the console UI

House-style hand-authored IIFE (no build step), extending the existing queue/detail:
- Detail view action area by status: priced → Approve form (weight/dims/price-override,
  confidence block prominent, ack control when non-high/high) + Reject;
  approved → Publish (prepare → payload review → confirm per checkpoint); draft →
  price entry (ManualComps via price override path or a manual-comps input);
  listed/sold/shipped/rejected → read-only provenance.
- Health strip at top: token freshness, env state, credits — compact, only loud when
  something is wrong (matches the "badge only when non-high" philosophy).
- Every mutation optimistically refreshes the queue and shows the server's structured
  error verbatim on failure.

### 3. Tests

plugin_api mutation endpoints tested in the existing plugin test style (stub client,
zero network): approve happy/low-conf-409/ack paths, reject, prepare returns payloads,
apply respects the checkpoint decision's gate, health degrades gracefully when the
refresh log or credits helper is absent. Existing suites stay green.

## Checkpoints (both BEFORE writing the affected code)

1. `publish-confirm-boundary` — does publish-apply live in the UI at v1?
2. `low-confidence-ux` — how the ack manifests in the UI.

## Acceptance

- All writers remain approve.py/publish.py/pricing.py — the plugin adds NO second
  writer of status or markers.
- A low-confidence approve without the ack is impossible through the UI, and the ack
  is always recorded (same audit contract as the CLI flag).
- Zero network in tests. Existing 81 tests keep passing.
- No changes outside `dashboard-plugins/listings/` except tests wiring if the house
  test layout requires it.
