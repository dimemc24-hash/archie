# Spec: Antiques listing pipeline — the body (eBay connections pending)

## Why

Morley photographs antiques; Archie already identifies and appraises them in Telegram chat
(`archie-visual-appraisal` skill + native glm-4.6v vision enrichment). What's missing is
everything downstream: durable listing state, a "list it" capture step, pricing research,
a human approval gate, publish-to-eBay, and label-on-sale. eBay developer approval is
pending, so every eBay touchpoint is built as a **provider seam**: real request shapes,
fixture-tested, cleanly refusing until keys exist. Nothing in this build calls eBay.

## State model (Supabase, Archie's project via REST — service key from env)

Ship `antiques/migration/001_listings.sql` (applied by Morley in Studio — do NOT try to
run DDL from code):

- `listings`: `id uuid pk default gen_random_uuid()`, `created_at/updated_at timestamptz`,
  `status text` CHECK in `draft|priced|approved|listed|sold|shipped|rejected|error`,
  `source text`, `title text`, `description text`, `category_guess text`,
  `appraisal jsonb`, `photos jsonb`, `pricing jsonb`, `approval jsonb`,
  `provider jsonb`, `shipping jsonb`, `notes text`.
- Enable RLS with NO anon policies (service-key access only). Add an
  `updated_at` trigger.
- Photos live in a private Storage bucket `listing-photos`; rows reference
  `{"bucket": "listing-photos", "path": "<listing-id>/<n>.jpg"}` objects.

## Components

1. **`antiques/common.py`** — stdlib-only Supabase REST client (PostgREST + Storage):
   base URL + service key from env `ARCHIE_SUPABASE_URL` / `ARCHIE_SUPABASE_SERVICE_KEY`;
   helpers: insert/patch/select on `listings`, `ensure_bucket()`, `upload_photo()`,
   `signed_url()`. Injectable transport (tests stub it; NO network in tests). Status
   transition guard: a single `advance(row_id, from_status, to_status, patch)` that
   refuses illegal jumps.
2. **`skills/antiques-capture/`** — the SANDBOX-side skill (runs in the docker default
   profile: stdlib only, no host FS, only `ARCHIE_SUPABASE_*` creds).
   `scripts/capture.py`: args or stdin-JSON with title/description/category/appraisal
   JSON + local photo paths (from the chat turn) + optional caption; uploads photos to
   Storage, inserts a `draft` row, prints the listing id + a one-line summary for the
   chat reply. `SKILL.md`: instructs the agent — after an `archie-visual-appraisal`
   flow, when Morley says "list it" (or similar), assemble the fields from the appraisal
   and call capture.py; confirm the listing id back; never invent fields the appraisal
   didn't establish (leave null).
3. **`antiques/pricing.py`** — `CompsProvider` protocol; implementations:
   `EbayBrowseComps` (real Browse-API search request/parse shapes, but constructor
   requires env `EBAY_APP_ID`/`EBAY_OAUTH_TOKEN` — missing → raise `NotConnected` with a
   friendly message), `ManualComps` (operator-supplied list). `price_listing(row_id,
   provider)` → writes `pricing` jsonb (comps, recommended price + range, method,
   priced_at) and advances `draft → priced`.
4. **`antiques/approve.py`** — `approve(row_id, weight_oz, dims, price_override=None)`
   validates status `priced` (or `draft` with manual price), records approval jsonb +
   shipping inputs, advances to `approved`, and writes a **pending-publish marker**
   (`~/harness/artifacts/antiques/<id>/pending-publish.json`, same digest idea as
   run_stage4: row hash + price + photo count). `reject(row_id, reason)`.
5. **`antiques/publish.py`** — `ListingProvider` protocol: `publish(row) -> provider_ids`.
   `EbayProvider`: builds the REAL Sell Inventory API sequence (inventory item → offer →
   publish; see kb/ebay-cheatsheet.md) as request dicts, via an injectable HTTP transport;
   without `EBAY_*` env → `NotConnected`. `DryRunProvider`: records what WOULD be sent.
   CLI: `publish.py --id <row> --apply` re-validates the marker (stale → refuse, exactly
   the stage-4 semantics) then calls the provider and advances `approved → listed`.
6. **`antiques/fulfill.py`** — worker skeleton run by future cron: `poll_orders(provider)`
   → for each sold listing: advance `listed → sold`, `LabelProvider.buy_label(row)`
   (protocol; `EbayLabels` + `Shippo` both `NotConnected` stubs with real request shapes),
   store label URL + tracking in `shipping`, advance `sold → shipped`, notify via
   `~/harness/alert.sh` (message includes label link). `--once --dry-run` supported.
7. **`dashboard-plugins/listings/dashboard/`** — manifest + IIFE `dist/index.js` +
   `plugin_api.py` (house pattern): queue view grouped by status with counts, listing
   detail (fields + signed photo URLs), read-only. Server-side reads env from
   `~/.hermes/.env` the same way other host code does.
8. **`antiques/test_antiques.py`** — pytest, zero network: stubbed REST transport; status
   machine legal/illegal transitions; capture round-trip (photos "uploaded", row shape);
   pricing with ManualComps + EbayBrowseComps NotConnected without env; approve → marker
   → publish --apply happy path with DryRunProvider; stale marker refused; fulfill
   dry-run advances and formats the alert; eBay payload shapes match
   `test fixtures (golden dicts)`.

## Checkpoints (consult before implementing)

- `image-persistence` — when/where photos become durable (capture-time Storage upload vs
  deferring), given the sandbox container's ephemeral FS and eBay needing images later.
- `capture-boundary` — how much the sandboxed capture skill does itself vs writing a
  queue row for a host worker to enrich (the spec-intake bridge precedent).

## Acceptance (hermes venv python)

- `py_compile` clean on every new .py; `pytest antiques/test_antiques.py -q` green;
  `python3 port/test_port.py` still 100/100.
- `capture.py --help` runs under PLAIN `python3` with no third-party imports (sandbox
  constraint proven by `python3 -c "import ast..."` check or equivalent).
- All three `NotConnected` paths produce actionable messages naming the missing env vars.
- Migration SQL parses (`python3 -c` with sqlparse NOT available — a comment-header +
  visual structure is enough; do not add dependencies).
- Diff scoped to `antiques/**`, `skills/antiques-capture/**`,
  `dashboard-plugins/listings/**`.
