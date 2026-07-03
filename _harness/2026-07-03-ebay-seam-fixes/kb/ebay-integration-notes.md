# eBay sandbox integration — live findings, 2026-07-03

Ground truth from the real connect session. Every error below was hit live; every "works"
shape published sandbox listing 110589745052. Use these as fixture data in tests.

## Working publish sequence (all three steps verified)

Headers on every JSON request (missing Content-Language → errorId 25709):
```
Authorization: Bearer <EBAY_OAUTH_TOKEN>
Content-Type: application/json
Content-Language: en-US
X-EBAY-C-MARKETPLACE-ID: EBAY_US
Accept: application/json
```

1. `PUT /sell/inventory/v1/inventory_item/{sku}` → 204
2. `POST /sell/inventory/v1/offer` → 201, body has `offerId`
3. `POST /sell/inventory/v1/offer/{offerId}/publish` → 200, body has `listingId`

## Observed errors (exact bodies for test fixtures)

Aspects as bare string (`{"Type": "Antiques"}`):
```
400 {"errors":[{"errorId":2004,"domain":"ACCESS","category":"REQUEST","message":"Invalid request","longMessage":"The request has errors. For help, see the documentation for this API.","parameters":[{"name":"reason","value":"Could not serialize field [product.aspects.Type]"}]}]}
```
Works: `{"Type": ["Antiques"]}` — every aspect value a list of strings.

Missing Content-Language:
```
400 {"errors":[{"errorId":25709,"domain":"API_INVENTORY","subdomain":"Selling","category":"Request","message":"Invalid value for header Content-Language."}]}
```

Empty imageUrls:
```
400 {"errors":[{"errorId":25717,"domain":"API_INVENTORY","subdomain":"Selling","category":"Request","message":"imageUrls cannot be null or empty.","parameters":[{"name":"text1","value":"imageUrls"}]}]}
```

Non-leaf category ("1") at publish step:
```
400 {"errors":[{"errorId":25005,"domain":"API_INVENTORY","subdomain":"Selling","category":"Request","message":"The eBay listing associated with the inventory item, or the unpublished offer has an invalid category ID. The category selected is not a leaf category."}]}
```

## Supabase signed URLs

`POST /storage/v1/object/sign/{bucket}/{path}` with `{"expiresIn": N}` returns:
```
{"signedURL": "/object/sign/listing-photos/<id>/0.jpg?token=..."}
```
The path is relative to `/storage/v1`. Joining `project_url + signedURL` → **404**
(verified). Correct: `project_url + "/storage/v1" + signedURL` → 200 (verified, and eBay
accepted the resulting https URL as an image).

## ensure_bucket duplicate (bucket exists)

Supabase wraps the conflict as HTTP 400 with this body — match on the body, not the code:
```
400 {"statusCode":"409","error":"Duplicate","message":"The resource already exists"}
```

## Taxonomy suggestions (category resolution reference)

`GET /commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q=candlestick`
(user-token Bearer auth works). Response shape (truncated to the used fields):
```
{"categorySuggestions":[{"category":{"categoryId":"4062","categoryName":"Candle Holders"},"categoryTreeNodeLevel":3,"categoryTreeNodeAncestors":[{"categoryId":"13777","categoryName":"Decorative Collectibles"},{"categoryId":"1","categoryName":"Collectibles"}]}, ...]}
```
categoryId 4062 was publish-verified end-to-end.

## Duplicate offer on retry

After a step-3 failure the offer survives; re-running POST /offer for the same SKU fails
(duplicate, errorId 25002 family; eBay's error body references the existing offer). Live
recovery was manual DELETE then re-run — the fix should recover the offerId and PUT-update
instead.

## Business-policy prerequisite (already done on this account, relevant for prod later)

Fresh sellers get `errorId 20403 "User is not eligible for Business Policy"` from the
Account API until: `POST /sell/account/v1/program/opt_in` body
`{"programType": "SELLING_POLICY_MANAGEMENT"}` → 200 (empty body).

## Host environment state (env NAMES only — values live in ~/.hermes/.env)

All set and verified live: EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_API_BASE
(sandbox), EBAY_RU_NAME, EBAY_OAUTH_TOKEN, EBAY_REFRESH_TOKEN,
EBAY_MERCHANT_LOCATION_KEY (archie-br-01), EBAY_FULFILLMENT_POLICY_ID,
EBAY_PAYMENT_POLICY_ID, EBAY_RETURN_POLICY_ID. Production keyset exists but is disabled
by eBay pending marketplace-account-deletion compliance — `EBAY_API_BASE` stays sandbox.

Access-token refresh is ALREADY AUTOMATED on the host: `~/.hermes/ebay-refresh.sh` via
crontab `*/30 * * * *`, re-mints EBAY_OAUTH_TOKEN from EBAY_REFRESH_TOKEN against
`$EBAY_API_BASE/identity/v1/oauth2/token`, logs to `~/.hermes/ebay-refresh.log`. The
README should document it, not re-create it.

## Approve-marker gotcha (do NOT "fix" — by design)

The pending-publish marker digests title + price + photo count. Adding a photo AFTER
approve stales the marker and publish refuses (correct behavior). Consequence for the
no-photos error in fix #3: photos must exist BEFORE approve; the error message should say
that, not suggest adding photos post-approval.
