# Build spec: eBay seam fixes — make EbayProvider publish unaided

## Context

On 2026-07-03 the eBay sandbox was connected for real (all `EBAY_*` env vars live on the
host) and a listing (eBay sandbox id 110589745052) was published end-to-end — but ONLY by
runtime-monkeypatching `antiques/publish.py` and `antiques/common.py`. The repo code as
merged fails at seven seams, each confirmed against the live sandbox API. This build fixes
all seven, with tests, so `publish.py --apply` works unaided. `kb/ebay-integration-notes.md`
has the exact observed errors and working request shapes — treat it as ground truth.

## Fixes (all confirmed live)

### 1. Aspects shape — `antiques/publish.py::_build_aspects`
eBay requires `product.aspects: dict[str, list[str]]`. Current code returns bare strings
(`{"Type": "Antiques"}`) → errorId 2004 `Could not serialize field [product.aspects.Type]`.
Wrap every value in a single-element list (values are always strings today).

### 2. Content-Language header — `antiques/publish.py::EbayProvider._headers`
The Sell Inventory API requires `Content-Language: en-US` on JSON-body requests; without
it → errorId 25709. Add it when `json_body=True`.

### 3. Real image URLs — `antiques/common.py::signed_url` + `antiques/publish.py`
Two halves, both required:
- `signed_url()`: Supabase's sign endpoint returns `{"signedURL": "/object/sign/<bucket>/
  <path>?token=..."}` — a path relative to `/storage/v1`. The current join
  (`self.url + signed`) produces a 404 URL. Correct join: `self.url + "/storage/v1" +
  signed` when the response is relative; leave absolute responses untouched.
- `publish.py` currently sends `supabase://bucket/path` placeholders. At publish time the
  host-side path must resolve each photo to a real signed https URL. Give `EbayProvider`
  an optional injectable URL-resolver (host caller passes one backed by
  `SupabaseClient.signed_url`; the CLI wires this up); `DryRunProvider` keeps recording
  placeholders with zero network. If the row has NO photos, publish must fail up front
  with an actionable error (eBay hard-rejects empty `imageUrls`, errorId 25717) — do not
  let it reach the API.

### 4. Leaf category — `antiques/publish.py::_guess_category_id`
Fallback `"1"` is rejected at publishOffer: errorId 25005 "not a leaf category".
**This is checkpoint `category-strategy` — STOP and ask before implementing.** Whatever
the council picks: failure mode must be an actionable error naming exactly what to set,
resolution must go through an injectable transport, and the verified-working reference is
Taxonomy `GET /commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q=<term>`
(user token auth; suggestion #1 for "candlestick" = 4062, publish-verified).

### 5. Retry-safe offer creation — `antiques/publish.py::EbayProvider.publish`
A publish that fails AFTER offer creation (exactly what happened live: category rejection
at step 3) strands an offer; re-running then fails at step 2 because an offer already
exists for the SKU (errorId 25002). On duplicate-offer, recover the existing offerId
(eBay's error body carries it; otherwise `GET /sell/inventory/v1/offer?sku=<sku>`),
update it (`PUT /sell/inventory/v1/offer/{offerId}`), and continue to publish.

### 6. Provider selection — `antiques/publish.py::main`
`--dry-run-provider` is `store_true` with `default=True`: it can never be False, so
`EbayProvider` is unreachable from the CLI. Replace with `--provider {dryrun,ebay}`,
default `dryrun`. Remove the dead flag; note the change in README.

### 7. `ensure_bucket` 409 tolerance — `antiques/common.py`
Duplicate bucket must be success, not an exception. Observed live: Supabase wraps it as
HTTP 400 with body `{"statusCode":"409","error":"Duplicate","message":"The resource
already exists"}` — detect via the body's statusCode/error, not just the HTTP code.

## README updates

`antiques/README.md`: connect checklist is now LIVE on the host — note that; add
`EBAY_RU_NAME` and `EBAY_DEV_ID` rows to the env table; document the access-token
refresh cron that is ALREADY INSTALLED on the host (`~/.hermes/ebay-refresh.sh`, crontab
`*/30`, re-mints `EBAY_OAUTH_TOKEN` from `EBAY_REFRESH_TOKEN`, follows `EBAY_API_BASE`);
document `--provider` replacing `--dry-run-provider`.

## Acceptance

- Every fix has a test reproducing the live failure shape (assert on request payloads /
  headers / URLs via injectable transports) and proving the fix.
- ZERO network in tests — house rule, no exceptions.
- All existing tests keep passing (39 passed as of 2026-07-03). Do not change the status
  machine or the approve-marker semantics.
- `python3 -m antiques.publish --id X` (dryrun) output shape unchanged apart from real
  image URLs replacing placeholders when a resolver is present.
