# NewChapter → OpenRouter + Priority-Hierarchy Migration (Spec v0)

**Date:** 2026-07-15 · **Directive:** Morley — "run everything through openrouter, and migrate ALL functions to the priority hierarchy that's been built. Trying to keep track of multiple platforms is going to cause a problem."
**Recommended execution:** Archie harness (Stage 1 spec → build → swarm → gate) against the NewChapter repo. This is the first formal flywheel job.

## Current state (recon 2026-07-15)

- All ~30 agents + Janet/Conductor call Anthropic directly: `lib/ai/client.ts` (`MODELS = {opus, sonnet, haiku}` → `claude-*`), provider hardcoded `'anthropic'` in `lib/agents/runner.ts:46,79`.
- Single-provider failure mode is proven: the 2026-07-03 Anthropic billing outage silently killed 54/60 document-review jobs (checklist matching dead for 12 days; no alert, no retry).
- Escape hatch exists: `aiClient()` honors `AI_GATEWAY_API_KEY`/`AI_GATEWAY_BASE_URL` — but OpenRouter's native API is OpenAI-format, so the Anthropic SDK path needs replacement, not just re-pointing.

## Target state

1. **Single gateway: OpenRouter.** `structuredCall`/`textCall` reimplemented on OpenRouter's API (JSON-schema via structured outputs / tool-call format). One key, one bill, one dashboard. `ANTHROPIC_API_KEY` retired from the app (Anthropic models still reachable *through* OpenRouter where a tier demands them).
2. **Priority hierarchy ported from the harness** (`port/harness_routing.py` precedence: red_zone > retry > blast_radius > stuck, adapted per-agent):
   - **Labor tier (default):** GLM (z-ai/glm-5.2) for classification, extraction, sync-class, summarization — document-review, pacer-tracker, fax-parser, call-processing, checklist matching, librarian.
   - **Drafting tier:** DeepSeek/Kimi-class for drafting agents (plan drafter, schedules drafter, POC analysis prose).
   - **Red-zone tier:** legal-critical functions escalate (means test math narratives, plan feasibility, POC objection grounds, anything touching `lib/plan`/`lib/income`/`lib/poc` outputs headed to court) — premium model via OpenRouter; keep the `consume_workorder.py` ESCALATE_KEYWORDS/LEGAL_PATH pattern as the escalation trigger vocabulary.
   - **Retry escalation:** a failed/garbled structured call retries one tier up, not same-tier.
3. **Ledger + circuit breaker ported** from `port/harness_ledger.py` + `port/openrouter_credits.py` (TypeScript): daily cost cap, per-agent caps, credits-floor "speedometer" check before batch fan-outs, Telegram/needs-you alert on breaker trip. This removes the fly-blind-through-outage failure mode permanently.
4. **Retry/backfill:** transient failures (billing, 429, 5xx) auto-requeue with backoff; a backfill command reprocesses `agent_jobs` in `error` status after recovery (one-time: the 2026-07-03 window).
5. **Meter records** gain a real `provider`/`model` per call (drop the hardcoded `'anthropic'`).

## Config shape

`model-routing.json` (DB or repo config, console-editable like the harness's `council-config.json`): per-agent `{tier, overrides}`, per-tier `{model, fallback, maxCostPerRun}`, global `{dailyCap, creditsFloor, alertChannel}`.

## Migration order (each step shippable)

1. OpenRouter client + tier map behind a flag; document-review (highest volume, lowest risk, already broken once) cut over first on GLM.
2. Remaining labor-tier agents; verify with selfchecks + a golden-output comparison batch (GLM vs prior Sonnet outputs on ~20 real documents, reviewed before cutover).
3. Drafting tier + Janet/Conductor.
4. Red-zone tier last, with the escalation vocabulary wired.
5. Ledger/breaker/alerting + retry/backfill; retire `ANTHROPIC_API_KEY`.

## Related approved work (same build or fast follow)

- **Deterministic-first checklist matching with review gate** (approved 2026-07-15): filename/term match first; a small-context model pass (labor tier) confirms before a checklist item flips, so a mis-filed document doesn't auto-match; classifier only for ambiguous docs.
- Vestigial `case_events.processed` column: drop or wire it — a 64k-row always-false column is a misleading health signal.
