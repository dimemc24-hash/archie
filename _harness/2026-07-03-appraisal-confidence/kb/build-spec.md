# Build spec: appraisal confidence — surface it at the human gates

## Context

The `archie-visual-appraisal` skill (live agent config, NOT in this repo) now emits a
mandatory confidence block in the appraisal JSON it hands to `antiques-capture`:

```json
"confidence": {
  "id": "high|medium|low|unknown",
  "value": "high|medium|low|unknown",
  "basis": "one line: what was verified vs assumed",
  "flags": ["authenticity-unverified", "serial-unread", "no-comps"]
}
```

`capture.py` already passes the appraisal dict through opaquely into the listing row's
`appraisal` jsonb — **no sandbox code change is needed or wanted**. This build makes the
confidence visible and consequential at the two human gates: the approve step and the
dashboard Listings tab. Morley's rule, verbatim: **"I'd rather 'I don't know' than
wrong."** Missing confidence (old rows, skill misfire) must be treated as `unknown`, not
as passing.

## Deliverables

### 1. `antiques/approve.py` — confidence at the approve gate

Add a small pure helper `appraisal_confidence(row) -> dict` that reads
`row["appraisal"]["confidence"]` and normalizes: absent block / absent keys / unrecognized
values → `{"id": "unknown", "value": "unknown", ...}` (never crash on malformed data).

`approve()` behavior on non-high confidence is **checkpoint `low-confidence-approve`** —
STOP and consult before implementing. Whatever is chosen: the confidence summary is
recorded in the `approval` jsonb (so it survives into history), and the human-facing
behavior follows the council's synthesis exactly.

### 2. `dashboard-plugins/listings/dashboard/` — confidence badge

Queue view: a compact per-row badge when confidence is non-high (e.g. `id:low` /
`val:unknown`) — nothing shown for high/high (badge noise devalues the signal).
Detail view: full block — id, value, basis line, flags. Match the existing house style in
`dist/index.js` (hand-authored IIFE, no build step) and `plugin_api.py` (server-side may
need to include the appraisal confidence in the listing payload if it doesn't already
pass the full appraisal jsonb through).

### 3. `skills/antiques-capture/SKILL.md` — document the field

One short section: the confidence block arrives inside the appraisal JSON, capture passes
it through untouched, downstream gates read it. No script changes.

### 4. Tests

- `appraisal_confidence()` normalization: present/absent/partial/malformed (string where
  dict expected, unknown enum values).
- approve-gate behavior per the checkpoint decision (both the low/unknown path and the
  high/high path).
- Approval jsonb records the confidence summary.
- Plugin server-side payload includes confidence (if plugin_api.py changes).
- Zero network, injectable transports, house test style.

## Acceptance

- All existing tests keep passing (54 as of the seam-fix merge).
- A row with no confidence block behaves exactly like `unknown/unknown` everywhere.
- `_harness/<run-id>/` artifacts aside, no files outside the deliverables list change.
