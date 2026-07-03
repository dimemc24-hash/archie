---
name: antiques-capture
description: "After an archie-visual-appraisal flow, when Morley says 'list it' (or similar), capture the item as a draft listing — upload photos to Supabase Storage and write the draft row. Runs in the sandbox (stdlib only, Supabase REST only)."
version: 1.0.0
author: Archie
created: "2026-07-03"
tags: [archie, antiques, listing, capture, sandbox]
---

# Antiques Capture

After an `archie-visual-appraisal` flow, when Morley says "list it" (or
similar — "put this up," "sell this," "add it"), assemble the fields from the
appraisal and call `capture.py` to create a draft listing.

## When to load

- Morley has just received an appraisal (via `archie-visual-appraisal`) and
  responds with a list/sell intent.
- The photos from the appraisal turn are still in the chat (capture.py reads
  them from local paths the gateway provides).

## When NOT to load

- Morley is still appraising — wait for the explicit "list it."
- Morley asks about pricing specifically — that's `antiques/pricing.py`, a
  host-side step, not this skill.
- No photos exist — a listing without photos is not useful; tell Morley to
  photograph the item first.

## How to call capture.py

```bash
python3 scripts/capture.py \
    --title "Vintage Omega Seamaster, ca. 1968" \
    --description "Manual-wind Seamaster, cal. 601, stainless 35mm..." \
    --category "Watches" \
    --appraisal '{"quick_id": "Omega Seamaster", "era": "1968", ...}' \
    --photo /tmp/chat/photo_0.jpg \
    --photo /tmp/chat/photo_1.jpg
```

Or via stdin (preferred for complex fields):

```bash
echo '{"title":"...","description":"...","appraisal":{...},"photos":["/tmp/.../0.jpg"]}' | \
    python3 scripts/capture.py --stdin
```

Use `--json-output` for machine-readable results (when chaining into another
tool call).

## Field rules

- **title**: from the appraisal's Quick ID. Never invent — if the appraisal
  didn't establish a clear title, ask Morley.
- **description**: from the appraisal's detail table + background. Keep it
  factual.
- **category_guess**: from the appraisal if stated (Watches, Cards, Furniture,
  etc.). Leave null if uncertain.
- **appraisal**: the full appraisal JSON (pass it through, don't restructure).
- **photo paths**: the local file paths the gateway gave you for the chat's
  photos. List them in order (best photo first).

## What capture.py does (so you can explain it)

**Capture boundary (council decision, checkpoint `capture-boundary`: full
capture — option a):** capture.py does everything in one shot — uploads
photos, writes the complete draft row (title/description/appraisal/category),
no host involvement until pricing. The sandbox already holds the Supabase
service key, so a host-side worker or quarantine adds latency and complexity
without reducing exposure. Human review before pricing is the sole quality
gate. No host-side validation pass — this keeps the capture path single-shot
and simple.

1. Inserts a draft listing row (status `draft`) to get a listing id.
2. Uploads every photo to the private Supabase Storage bucket at
   `<listing-id>/<index>.jpg` — durable immediately.
3. If a photo upload fails: the raw bytes are base64-encoded and stored in
   the listing's `notes` field (durable in Postgres) before capture.py
   returns. A host-side retry worker can recover them later. The listing is
   still created as a draft — a failed upload degrades to a draft with a
   noted gap, never a lost listing.
4. Patches the listing row with the photo references.
5. Prints the listing id + a one-line summary.

## After capture

- Confirm the listing id back to Morley.
- Tell him the next step is pricing (host-side, not this skill).
- If any photos failed, mention it: "1 photo failed upload — it's buffered
  for retry, not lost."

## Pitfalls

- **Never invent fields the appraisal didn't establish.** If condition,
  era, or valuation is missing, leave it null. Morley reviews before
  publish — but garbage in the draft wastes his review time.
- **Photos must be local paths.** This runs in the sandbox; it can't fetch
  URLs. The gateway provides local file paths for chat photos — use those.
- **The sandbox is ephemeral.** If capture.py crashes mid-flow, the draft
  row is still in Postgres (durable). But un-uploaded photos are NOT —
  which is why capture.py buffers failed uploads to notes immediately.
- **Never print the Supabase service key.** capture.py reads it from env;
  never echo it in messages or logs.
