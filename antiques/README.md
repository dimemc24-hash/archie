# Antiques Listing Pipeline

End-to-end pipeline for Morley's antiques business: photograph → appraise →
capture → price → approve → publish to eBay → fulfill → ship.

## Architecture

```
Telegram chat (photo + "what is this?")
  │
  ▼
archie-visual-appraisal skill (vision + research → appraisal JSON)
  │
  ▼  "list it"
capture.py (SANDBOX: stdlib only, Supabase REST only)
  │  ─ uploads photos to Storage at capture time (durable immediately)
  │  ─ inserts draft listing row
  │  ─ on photo upload failure: base64-buffers bytes into notes (durable)
  ▼
draft row in Supabase `listings` table
  │
  ▼  pricing.py (host-side)
priced row (comps + recommended price)
  │
  ▼  approve.py (host-side, human gate)
approved row + pending-publish marker
  │
  ▼  publish.py --apply (host-side, re-validates marker)
listed row (provider ids stored)
  │
  ▼  fulfill.py (host-side cron, when an order arrives)
sold → shipped (label purchased, tracking stored, Morley alerted)
```

## Council decisions (checkpoints)

### image-persistence — full capture-time upload

Photos are uploaded to the private Supabase Storage bucket at capture time
(durable immediately). This avoids Telegram file_id expiry and the need for a
host-side re-fetch worker. On upload failure, the raw bytes are base64-encoded
and stored in the listing's `notes` field (durable in Postgres) before the
sandbox exits — a host-side retry worker can recover them later. A failed
upload degrades to a draft row with a noted gap, never a lost listing.

### capture-boundary — full capture (option a)

The sandboxed capture skill does everything itself: uploads photos, writes the
complete draft row (title/description/appraisal), one-shot, no host involvement
until pricing. The sandbox already holds the Supabase service key, so a
host-side worker or quarantine adds latency and complexity without reducing
exposure. Human review before pricing is the sole quality gate — no host-side
validation pass. This keeps the capture path single-shot and simple.

### category-strategy — resolve leaf category at pricing time (option b)

eBay `publishOffer` rejects non-leaf category IDs (errorId 25005); the old
fallback `"1"` is a top-level node. The leaf category is now resolved at
**pricing time** via Taxonomy `get_category_suggestions` and stored as
`category_id` + `category_name` in the `pricing` jsonb. This surfaces the exact
category at the human approve gate — the pipeline's only quality check — so
Morley can verify or correct it before publish. Publish stays deterministic and
free of network surprises (it reads `pricing.category_id`, no API call).

When resolution returns nothing, `price_listing` raises
`CategoryNotResolvedError` — an actionable error telling the operator to set
`category_guess` or manually set `pricing.category_id`. Never a silent default.

## State model (Supabase)

Table: `listings` (see `migration/001_listings.sql`)

```
status: draft → priced → approved → listed → sold → shipped
        draft → approved            (manual price at approve time)
        any   → rejected | error    (terminal)
```

RLS is ON with NO anon policies — service-key access only.

### Migration

Paste `migration/001_listings.sql` into Supabase Studio → SQL Editor → Run.
Morley applies this by hand; code never runs DDL.

## Components

### antiques/common.py

Stdlib-only Supabase REST client (PostgREST + Storage). Shared by all
host-side modules. Injectable transport for zero-network tests. Status machine
`advance()` is the only writer of `status` — it uses a conditional
`status=eq.<from>` filter so concurrent changes are detected.

### skills/antiques-capture/

The SANDBOX-side skill (runs in Docker default profile: stdlib only, no host
FS, only `ARCHIE_SUPABASE_*` creds). `capture.py` creates draft listings and
uploads photos. See `skills/antiques-capture/SKILL.md` for the agent workflow.

### antiques/pricing.py

`CompsProvider` protocol with:
- `ManualComps` — operator-supplied (no network, for testing/manual pricing)
- `EbayBrowseComps` — real eBay Buy Browse API request shape; `NotConnected`
  without `EBAY_OAUTH_TOKEN`
- `SoldComps` — stub (needs restricted Marketplace Insights API)

`TaxonomyResolver` protocol with:
- `EbayTaxonomyResolver` — calls eBay Taxonomy
  `get_category_suggestions?q=<term>` (user-token auth, injectable transport);
  takes the top suggestion's leaf `categoryId` (verified: 'candlestick' → 4062).
  `NotConnected` without `EBAY_OAUTH_TOKEN`.

`price_listing(row_id, provider, taxonomy_resolver=None)` writes the `pricing`
jsonb (comps, median price + ±25% range, method, priced_at) and advances
`draft → priced`. When a `taxonomy_resolver` is passed, it also resolves and
stores `category_id` + `category_name` in the pricing jsonb — surfacing the
leaf category at the approve gate. If resolution returns nothing, raises
`CategoryNotResolvedError` (actionable, never a silent default to `"1"`).

### antiques/approve.py

Human approval gate:
- `approve(row_id, weight_oz, dims, price_override=None)` — validates status,
  records approval jsonb, advances to `approved`, writes a pending-publish
  marker to `~/harness/artifacts/antiques/<id>/pending-publish.json`.
- `reject(row_id, reason)` — advances to `rejected` with reason in notes.

The marker is keyed to a digest of the row's title + price + photo count. If
the row changes between approve and publish, the marker is stale and publish
refuses — same two-step semantics as `run_stage4.py`.

### antiques/publish.py

`ListingProvider` protocol with:
- `DryRunProvider` — records what would be sent (no network)
- `EbayProvider` — real eBay Sell Inventory API sequence (inventory item →
  offer → publish); `NotConnected` without `EBAY_OAUTH_TOKEN`,
  `EBAY_MERCHANT_LOCATION_KEY`, and the three policy IDs

`EbayProvider` accepts an optional `url_resolver` callable that resolves
Storage photo refs to signed https URLs at publish time (the CLI wires one
backed by `SupabaseClient.signed_url`). Without photos, publish fails up
front with an actionable error (eBay rejects empty `imageUrls`, errorId 25717).

The offer-creation step is retry-safe: on a duplicate-offer error (25002
family, from a failed prior run), it recovers the existing `offerId` from the
error body or via `GET /offer?sku=<sku>`, PUT-updates it, and continues to
publish — no stranded offers.

The `categoryId` in the offer payload is read from `pricing.category_id`
(resolved at pricing time). If missing, publish raises an actionable error —
never a silent default to `"1"` (non-leaf, errorId 25005).

CLI:
```bash
# Dry-run (default): show what would be sent
python3 -m antiques.publish --id <listing-id>

# Actually publish (re-validates marker, advances approved → listed)
python3 -m antiques.publish --id <listing-id> --apply --provider ebay
```

`--provider {dryrun,ebay}` selects the provider (default: `dryrun`). This
replaces the old `--dry-run-provider` flag which was unreachable.

### antiques/fulfill.py

Worker skeleton for future cron:
- `EbayOrderProvider` — polls eBay Fulfillment API for new orders
- `LabelProvider` protocol: `EbayLabels` (NotConnected stub), `Shippo`
  (NotConnected stub with real request shape), `DryRunLabelProvider`
- `fulfill_pass()` — polls orders, advances `listed → sold`, buys labels,
  advances `sold → shipped`, alerts Morley via `~/harness/alert.sh`

CLI:
```bash
python3 -m antiques.fulfill --once --dry-run
```

### dashboard-plugins/listings/

Dashboard plugin (manifest + IIFE `dist/index.js` + `plugin_api.py`). Queue
view grouped by status with counts, listing detail (fields + signed photo
URLs). Read-only — server-side reads env from `~/.hermes/.env` like other host
code.

## Approval CLI examples

```bash
# Price a draft listing (host-side, with manual comps)
python3 -c "
from antiques.common import SupabaseClient
from antiques.pricing import ManualComps, price_listing
c = SupabaseClient()
price_listing('<listing-id>', ManualComps([{'price': 185.0}, {'price': 210.0}]), c)
"

# Approve a priced listing
python3 -c "
from antiques.common import SupabaseClient
from antiques.approve import approve
c = SupabaseClient()
approve('<listing-id>', weight_oz=5.0, dims={'l': 6, 'w': 4, 'h': 2}, client=c)
"

# Publish (dry-run first, then apply with the eBay provider)
python3 -m antiques.publish --id <listing-id>
python3 -m antiques.publish --id <listing-id> --apply --provider ebay

# Reject a listing
python3 -c "
from antiques.common import SupabaseClient
from antiques.approve import reject
c = SupabaseClient()
reject('<listing-id>', 'not authentic', client=c)
"
```

## eBay connect checklist

The eBay sandbox is **LIVE on the host** (connected 2026-07-03, sandbox listing
110589745052 published end-to-end). All env vars below are set in
`~/.hermes/.env`:

| Env var | Where it comes from |
|---|---|
| `EBAY_APP_ID` | eBay Developer Portal → Application keys |
| `EBAY_CERT_ID` | eBay Developer Portal → Application keys (cert) |
| `EBAY_DEV_ID` | eBay Developer Portal → Dev ID (for some API calls) |
| `EBAY_RU_NAME` | eBay Developer Portal → Ru Name (OAuth redirect) |
| `EBAY_REFRESH_TOKEN` | eBay OAuth2 user consent flow |
| `EBAY_OAUTH_TOKEN` | Minted from refresh token (auto-refreshed, see below) |
| `EBAY_API_BASE` | `https://api.sandbox.ebay.com` (sandbox; prod pending marketplace-account-deletion compliance) |
| `EBAY_MERCHANT_LOCATION_KEY` | eBay Sell Inventory → merchant location (archie-br-01) |
| `EBAY_FULFILLMENT_POLICY_ID` | eBay Sell → Account → fulfillment policies |
| `EBAY_PAYMENT_POLICY_ID` | eBay Sell → Account → payment policies |
| `EBAY_RETURN_POLICY_ID` | eBay Sell → Account → return policies |
| `SHIPPO_API_KEY` | Shippo dashboard → API keys (for labels) |

### Access-token refresh (already installed)

`~/.hermes/ebay-refresh.sh` runs via crontab `*/30 * * * *`, re-mints
`EBAY_OAUTH_TOKEN` from `EBAY_REFRESH_TOKEN` against
`$EBAY_API_BASE/identity/v1/oauth2/token`, and logs to
`~/.hermes/ebay-refresh.log`. This is already installed on the host — do not
re-create it.

Every `NotConnected` error names exactly which env vars are missing.

## Cron lines (install LATER)

```bash
# Pricing worker (optional — can also be triggered manually per listing)
# 0 */4 * * * cd ~/harness/workspaces/archie/repo && ~/.hermes/hermes-agent/venv/bin/python3 -m antiques.pricing_worker --once

# Fulfillment poller (commented — install when eBay is connected)
# */15 * * * * cd ~/harness/workspaces/archie/repo && ~/.hermes/hermes-agent/venv/bin/python3 -m antiques.fulfill --once >> ~/harness/logs/fulfill.log 2>&1
```

## Testing

```bash
# All tests (zero network — every HTTP path is stubbed)
~/.hermes/hermes-agent/venv/bin/python3 -m pytest antiques/test_antiques.py -q

# Capture.py is stdlib-only (sandbox constraint)
python3 -c "import ast; tree = ast.parse(open('skills/antiques-capture/scripts/capture.py').read()); print('stdlib check passed')"
python3 skills/antiques-capture/scripts/capture.py --help
```
