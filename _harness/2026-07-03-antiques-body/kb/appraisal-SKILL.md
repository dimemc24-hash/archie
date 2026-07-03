---
name: archie-visual-appraisal
description: "Identify, research, and appraise physical objects from photos — watches, cards, collectibles, furniture, machinery, anything Morley photographs. Multi-pass vision interrogation → background research → condition assessment → valuation. Load when the user sends a photo of a physical object and asks 'what is this' or 'tell me about this.'"
version: 1.0.0
author: Archie
created: "2026-07-03"
tags: [archie, vision, appraisal, identification, collectibles, research]
---

# Archie Visual Appraisal

When Morley sends a photo of a physical object and asks you to identify it,
tell him about it, or appraise it — this is the workflow.

## When to load

- User sends a photo of a physical object (watch, card, coin, instrument,
  furniture, tool, etc.) and asks "what is this," "tell me about this,"
  "how much is this worth," or similar.
- User sends a photo for estate valuation context (bankruptcy cases).
- Not for: screenshots, documents, diagrams, or pure-text images — those
  go through archie-vision or ocr-and-documents.

## Workflow

### 1. Multi-pass vision interrogation

Don't settle for the first description. The gateway already ran a generic
vision pass (the "[The user sent an image...]" preamble). Do additional
targeted passes with `vision_analyze`, each asking about a specific aspect:

- **Pass 1 — Broad**: "What is this object? Describe all visible details."
- **Pass 2 — Specifics**: Ask about exact markings, text, numerals, labels,
  hallmarks, signatures, dates, or model numbers. Be precise: "What exact
  text is visible? Read every number and letter you can see."
- **Pass 3 — Condition**: "Assess the condition — wear, damage, patina,
  modifications, missing parts."

Multiple passes surface details a single pass misses (e.g., the first pass
missed numerals 2 and 11 that the second pass caught).

### 2. Background research

Research the object's manufacturer, era, model line, and market context.
See `references/research-techniques.md` for sources that work from the
container (search engines often block the IP — use API endpoints instead).

### 3. Structured assessment

Deliver findings in this format:

1. **Quick ID** — what it is, era, manufacturer/brand
2. **Detail table** — feature-by-feature breakdown (use a Markdown table)
3. **Background** — manufacturer history, model line context
4. **Condition assessment** — honest evaluation of wear and functionality
5. **Valuation** — ballpark range with condition tiers (as-is / running /
   with extras). Be honest; most things aren't rare.
6. **Next steps** — what additional info would nail the ID (case back,
   serial number, etc.)

### 4. Tone

Morley is an operator — he wants substance, not padding. Give him the
facts, the table, the number, and move on. He'll ask follow-ups if he
wants more. Don't hedge excessively; if you're 80% sure, say so.

## Pitfalls

- **Single vision pass is insufficient.** The first description always
  misses fine details (small text, secondary numerals, sub-dial markings).
  Always do at least 2 targeted passes.
- **Don't claim rarity without evidence.** Most vintage consumer goods
  were mass-produced. Say "not rare" when that's the truth.
- **Valuation ranges should be honest.** If it's a $50 watch, say $35–$75,
  not "$200–$500." Morley does estate valuations for bankruptcy cases —
  inflated numbers are worse than useless.
- **Search engines block the container IP.** Don't burn turns retrying
  Google/DuckDuckGo/Bing. Go straight to API endpoints. See the
  references file.

## Support files

- `references/research-techniques.md` — research sources that work from
  the container, including the Wikipedia REST API pattern and search
  engine workaround strategies.

## Related skills

- `archie-vision` — the underlying vision mechanism (model, analyze()
  function, gateway routing). This skill is about *what to do with* vision
  for object identification; that skill is about *how vision works*.
- `archie-closer-look` — for high-stakes extraction where a wrong digit
  matters (e.g., reading a serial number that feeds a legal filing).
