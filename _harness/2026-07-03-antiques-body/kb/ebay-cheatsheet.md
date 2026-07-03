# eBay API cheat-sheet (for the provider seams — NO live calls in this build)

All endpoints below are the shapes `EbayProvider` / `EbayBrowseComps` / `EbayLabels`
must construct and fixture-test. Auth is OAuth2: app credentials (`EBAY_APP_ID` +
`EBAY_CERT_ID`) mint/refresh a user access token from `EBAY_REFRESH_TOKEN`; requests
carry `Authorization: Bearer <token>`. Sandbox base `https://api.sandbox.ebay.com`,
production `https://api.ebay.com` — make the base URL a constructor arg defaulting from
env `EBAY_API_BASE` (default sandbox: safety).

## Comps (pricing.py — Buy Browse API)

`GET /buy/browse/v1/item_summary/search?q=<query>&limit=25&filter=buyingOptions:{FIXED_PRICE}`
Headers: Bearer token + `X-EBAY-C-MARKETPLACE-ID: EBAY_US`.
Parse per item: `title`, `price.value`, `price.currency`, `condition`, `itemWebUrl`.
Comps output: list of dicts + `recommended` = median of prices with a ±25% range and
`method: "active-comps-median"`. (True SOLD comps need the restricted Marketplace
Insights API — leave a `SoldComps` stub raising NotConnected with that explanation.)

## Listing (publish.py — Sell Inventory API, in order)

1. `PUT /sell/inventory/v1/inventory_item/{sku}` — body:
   `{"product": {"title", "description", "imageUrls": [...], "aspects": {...}},
     "condition": "USED_EXCELLENT" (map from appraisal condition),
     "availability": {"shipToLocationAvailability": {"quantity": 1}}}`
   SKU convention: `archie-<listing-uuid>`.
2. `POST /sell/inventory/v1/offer` — body:
   `{"sku", "marketplaceId": "EBAY_US", "format": "FIXED_PRICE",
     "pricingSummary": {"price": {"value", "currency": "USD"}},
     "categoryId", "merchantLocationKey": env EBAY_MERCHANT_LOCATION_KEY,
     "listingPolicies": {"fulfillmentPolicyId", "paymentPolicyId", "returnPolicyId"}}`
   → returns `offerId`.
3. `POST /sell/inventory/v1/offer/{offerId}/publish` → returns `listingId`.

Store all three ids in the row's `provider` jsonb:
`{"kind": "ebay", "sku", "offer_id", "listing_id", "published_at"}`.
Image URLs: Supabase Storage signed URLs are acceptable v1 (long expiry); note in README
that eBay Picture Services (EPS) upload is the durable upgrade.

## Orders + fulfillment (fulfill.py — Sell Fulfillment API)

- Poll: `GET /sell/fulfillment/v1/order?filter=orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS}`
  — match orders to rows via SKU (lineItems[].sku).
- Tracking upload: `POST /sell/fulfillment/v1/order/{orderId}/shipping_fulfillment` —
  `{"lineItems": [{"lineItemId"}], "shippedDate", "shippingCarrierCode": "USPS",
    "trackingNumber"}`.

## Labels (LabelProvider seam)

- `EbayLabels`: NotConnected stub — note that eBay's Logistics API is limited-availability;
  shape: `POST /sell/logistics/v1/shipping_quote` then `POST .../create_from_shipping_quote`.
- `Shippo`: shape only — `POST https://api.goshippo.com/transactions/`
  (`{"shipment": {"address_from", "address_to", "parcels": [{weight/dims}]},
    "carrier_account", "servicelevel_token"}` → `label_url`, `tracking_number`), auth
  header `ShippoToken <SHIPPO_API_KEY>`. NotConnected without `SHIPPO_API_KEY`.

## NotConnected contract

`class NotConnected(RuntimeError)` carrying `.missing: list[str]` of env var NAMES; the
message tells the operator exactly what to set and where (README §eBay connect
checklist). Every provider constructor checks env eagerly so failures happen at
construction, not mid-flow.
