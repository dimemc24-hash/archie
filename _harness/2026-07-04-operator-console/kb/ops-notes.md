# Operator Console — ops notes (ground truth, 2026-07-04)

## Health-strip data sources (host-side, read from plugin_api.py)

- **Token freshness**: `~/.hermes/ebay-refresh.log` — cron appends
  `<ISO8601Z> OK re-minted (expires_in=7200)` every 30 min. Health = minutes since the
  last `OK` line; warn > 45 min, red > 120 min (token lifetime is 2h). Log absent →
  "refresh cron not installed" warning, not a crash.
- **eBay env**: presence booleans for EBAY_APP_ID, EBAY_CERT_ID, EBAY_OAUTH_TOKEN,
  EBAY_REFRESH_TOKEN, EBAY_MERCHANT_LOCATION_KEY, EBAY_FULFILLMENT_POLICY_ID,
  EBAY_PAYMENT_POLICY_ID, EBAY_RETURN_POLICY_ID, EBAY_API_BASE value's env
  (sandbox/production derived from the URL — the string "sandbox" in it). NAMES and
  booleans only.
- **Credits**: `port/openrouter_credits.get_credits()` (repo root on sys.path when the
  dashboard mounts the plugin — remember the `__file__` self-resolution house rule).
  Returns {total_credits, total_usage, remaining}. Any exception → {"available": false}.

## The ack contract (from the appraisal-confidence merge)

- `antiques.approve.approve(..., acknowledge_low_confidence=True)` is the ONLY way to
  approve non-high/high. It raises `LowConfidenceError` (importable from
  antiques.approve) otherwise; the error carries (id, value) effective confidence.
- Confidence normalization: `id`/`value` keys primary, `identification`/`valuation`
  legacy, missing → unknown. The queue payload already carries normalized
  `confidence: {id, value}` per row; detail rows carry the full appraisal jsonb.

## Publish two-step semantics (do not reimplement)

- `publish_listing(row_id, DryRunProvider(), apply=False)` → what-would-be-sent, no
  status change, no marker consumption.
- `publish_listing(row_id, EbayProvider(url_resolver=...), apply=True)` → re-validates
  the pending-publish marker (digest of title+price+photo count), publishes, advances
  approved→listed. Stale marker → refusal (that's the two-step gate working).
- EbayProvider needs the env + a url_resolver for photos; see how the CLI wires it in
  antiques/publish.py main().

## Current data

The listings table holds 3 labeled TEST rows (titles start "TEST Archie") in listed
status from the connect-day proofs — safe demo data; Morley will reject them eventually.
A dashboard reject button working against them is the natural smoke test.

## House gotcha

Modules mounted by the dashboard must resolve imports via their own `__file__` (bit
twice: fusion port/, listings plugin). plugin_api.py already does this — keep it.
