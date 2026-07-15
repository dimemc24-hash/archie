# Firm Agent — Design

**Date:** 2026-07-15
**Status:** Validated in brainstorm session with Morley; ready for implementation planning
**Companion docs:** [charter draft](2026-07-15-firm-agent-charter.md) · [action layer v0 spec](2026-07-15-action-layer-v0-spec.md) · [play runbooks](2026-07-15-play-runbooks.md)

## 1. The problem

NewChapter is doing well, but its interface has become a barrier rather than an accelerator for Morley specifically. His AI competency is exponentially higher than the sum of his staff's; a system designed for him-as-he-is-now won't work for them, and a system designed for them won't work for him. He needs an assistant that is his eyes, ears, and hands while he remains the decision-maker, judgment, and brain — without abandoning the staff, the app, or the eventual convergence between the two.

The audit of the three existing codebases showed the pieces already exist:

- **NewChapter** — the value is almost entirely UI-independent already: the income/means-test engine (`lib/income/engine.ts`), Ch.13 plan math (`lib/plan/`), the POC objection engine (`lib/poc/`), 17 form renderers (`/api/forms/[form]`), CM/ECF package generation (`lib/efile/`, transmission held), Graph/PACER/Phaxio/Twilio/trustee-portal integrations, ~30 UI-less background agents, and Janet/Conductor. `CLAUDE.md`'s "Janet carries the weight" and `docs/desk-vision.md` already articulate this pivot.
- **Archie (this repo)** — a generic 4-stage dev harness (spec → segmented build with Fusion council → swarm review → two-step gated merge) with cost circuit-breakers, plus the antiques business and a Hermes-runtime personal assistant.
- **Old Archie (`archie-old/`)** — a proven persona/charter (efficiency analyst + pitch mechanic), a 12-tool capability contract, a memory-with-approval schema, and the hard-won durable-notes lesson.

## 2. Decisions made (with rationale)

1. **External agent, NewChapter as toolbox.** The assistant runs as its own agent runtime, driving NewChapter's headless layer. Decouples the assistant from the app's deploy cycle and lets it span work outside NewChapter.
2. **Interactive-first, ambient second.** Phase 1 is interactive sessions (desktop/phone). Phase 2 adds an always-on channel (Telegram daemon) that reuses NewChapter's 30 background agents as sensors rather than duplicating them.
3. **Tiered trust.** Reads free. Internal writes autonomous but logged and attributable. Anything leaving the firm (email, fax, trustee portals, CM/ECF, external invites) requires explicit approval; an in-session direct order counts as the approval and is logged as such.
4. **The firm agent is NOT Archie.** Archie stays on his path (personal assistant, antiques, dev harness). The firm agent is built fresh, stealing Archie's proven patterns (two-step gates, cost ledger, phone-to-spec queue) and old Archie's charter DNA. Whether it is "Janet grown up" or a new name is deferred; the only argument for keeping the Janet identity is staff transition.
5. **The action layer lives in NewChapter, not in any agent.** See §3. This was the pivotal design move: the capabilities belong to the app as a validated tool surface, and every agent — the firm agent, Janet in-app, future staff agents — mounts the same tools.

## 3. Architecture — three actors

### 3.1 NewChapter action layer (the foundation)

An MCP server (or equivalent tool API) exposed by NewChapter, wrapping its **validated** write paths — the same server actions the UI calls, never raw table writes — plus the compute engines, form generation, and integrations. Every tool carries, by construction:

- **Validation** — the `party_type` CHECK-constraint / `county_fips` char(5) class of silent-failure bug becomes impossible; guardrails ship with the tool.
- **Write-back as architecture** — tools write to `case_notes`/`wiki_md` as a side effect of doing work; session knowledge cannot evade the case record because the tools are the case record.
- **One audit trail, one approval chokepoint** — outbound actions from any surface land in the same `approvals` queue. Approval consolidation moves below the interfaces instead of multiplying across them.
- **A contract** — MCP schemas plus per-group selfcheck scripts (extending the existing `scripts/*-selfcheck.ts` pattern) so schema migrations break loudly, not silently.

Authentication mimics the proven background-agent pattern (service-role client, firm-scoped, self-stamped `firm_id`/`case_id`).

### 3.2 The firm agent (name TBD)

A charter + the action-layer tools, mountable in any harness. **One brain, multiple doors, per-door guardrails:**

- **Now:** terminal/Claude Code sessions for Morley (widest door).
- **Now:** the in-app Janet chat remains for staff (narrower door, same tools underneath as the action layer absorbs the Conductor's toolset).
- **Later:** Telegram/ambient daemon; the desk.

This is the competency-gap strategy: the gap closes by opening doors, not by rebuilding systems. Desk-vision is not killed — the desk incrementally becomes the convergence surface where the agent's work is spatially visible and staff drive the same tools through UI.

Beyond the action layer, the agent's full mounts: Graph mail + calendar firm-wide, SharePoint/PACER documents, drafting at large (pleadings, letters, memos beyond templates; means-test and Schedule I skills), the 30 background agents' findings as sensors, and room for family law / firm management mounts later.

### 3.3 Archie (the builder)

Unchanged in identity. His 4-stage harness is the execution arm of the improvement flywheel: approved pitches become Stage-1 specs targeting the NewChapter repo, built with council checkpoints, swarm-reviewed, merged only through the two-step gate.

## 4. The improvement flywheel

A standing loop that keeps NewChapter improving for staff even while Morley stops using its screens — the structural answer to staff divergence and the split-brain risk:

1. **Friction log.** Whenever a tool call fails, data is missing, validation surprises, or a manual step repeats, the agent records a one-line entry (app, file paths, estimated minutes/week) in a durable backlog.
2. **Pitches, both directions.** The agent raises high-leverage fixes ("I have a pitch"); equally — and, expected, more often at first — Morley pitches the agent. Same backlog either way. Risk-tiered using the escalation logic already in `consume_workorder.py`.
3. **Approved pitch → harness executes.** Spec → build → swarm → gated merge. The agent is NewChapter's user *and* its developer, under the same approval discipline as everything else.

## 5. The four plays (build order)

Detailed runbooks in [play runbooks](2026-07-15-play-runbooks.md). Pipeline order matches case lifecycle:

1. **Pre-petition audit** — document inventory vs. required; pay stubs → income engine → CMI/means test/Schedule I-J with receipts; Louisiana exemptions pass; plan compute + floor tests; readiness memo with judgment calls staged and options priced.
2. **Case filing** — forms bundle render, CM/ECF package + creditor matrix, validation, package handed to Morley for ECF upload (transmission stays held — court constraint); on filing, capture case number/341 date, spawn deadlines and pre-341 tasks. **Built live against the 5 currently-unfiled cases.**
3. **Hearing/341 briefing** — trustee requirements check (Crawford: DSI, tax affidavit, payroll consent, gambling affidavit), missing-doc chase (gated), NDC/payment status, morning-of briefing memo per case.
4. **Confirmation work** — objections + plan drift in one picture; amended plans via `lib/plan`; POC objections via the objection engine; responses/amended pleadings drafted for edit; all outbound gated.

**Build order: Plays 1+2 together first** (the 5 cases force it; filing requires audit), **Play 3 second** (weekly recurrence = fastest flywheel data), **Play 4 third** (most drafting depth). Action layer v0 is scoped to only what Plays 1+2 need (~a dozen tools), per YAGNI.

## 6. Accepted losses and their mitigations

| Loss | Mitigation |
|---|---|
| Screens win at scan-and-spot work (dockets, registers, calendars) | NewChapter UI survives for those; desk-vision absorbed incrementally as convergence surface, not killed |
| Split brain (session knowledge outside the app) | Architectural, not disciplinary: action-layer tools write back to the case record by construction |
| Staff divergence | The flywheel: Morley's friction becomes app improvements staff receive |
| Bypassing UI validation | Tools wrap server actions, never raw tables — load-bearing constraint |
| Two systems to keep healthy | MCP contract + selfcheck scripts make breakage loud; migrations gate on them |
| Approval-surface proliferation | Single `approvals` queue beneath all surfaces; in-session orders auto-approve with attribution |

## 7. Open questions (deferred deliberately)

- **Naming/identity:** new agent vs. Janet-grown-up. Only transition value argues for Janet. Decide before staff-facing changes.
- **Repo home for the firm agent:** fresh repo vs. a directory in NewChapter vs. here. Leaning fresh repo once the action-layer spec stabilizes.
- **Ambient channel details** (Telegram vs. SMS vs. both; daemon hosting) — Phase 2.
- **Desk convergence design** — revisit `docs/desk-vision.md` once Plays 1–3 are running and the agent's work products have settled into shapes worth rendering.
- **Family law mounts** — no design yet; architecture must simply not preclude them (it doesn't).

## 8. Immediate next steps

1. Review this package; settle naming if ready.
2. Build action layer v0 (spec: [action layer v0 spec](2026-07-15-action-layer-v0-spec.md)) — candidate first job for the harness itself.
3. Maiden run: pick the readiest of the 5 unfiled cases and run Play 1 against it, building tools as reality demands them.
4. Rotate the leaked credentials in `archie-old\efficiency-agent-build\.env` and `...\server\.env` (live Anthropic/E2B/Supabase service-role keys, previously pushed to GitHub).
