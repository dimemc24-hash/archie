# NewChapter Action Layer — v0 Spec (Plays 1 + 2)

**Date:** 2026-07-15
**Companion to:** [design](2026-07-15-firm-agent-design.md) · [play runbooks](2026-07-15-play-runbooks.md)
**Grounding:** code recon of `D:\Apps_for_Git\FSFAI\nc-architecture\NewChapter` on 2026-07-15. All file:line citations refer to that repo.

## 0. Recon corrections to the design doc

1. **18 registered forms, not 17** (`lib/forms/registry.ts:44-65`): B101, B121, B106AB, B106C, B106D, B106EF, B106G, B106H, B106I, B106J, B106SUM, B107, B108, B122A1, B122A2, B122C1, B122C2, PROC. PROC is `standalone: true` (green paper, never merged into bundles).
2. **`case_parties.party_type` has NO CHECK constraint** — it is plain `text NOT NULL` (`supabase/migrations/0000_baseline_reference.sql:277`); the 4-value constraint claimed at `profile/actions.ts:91` is a comment, not DB reality, and `fetchCaseBundle` matches debtor2 against a *different, broader* value set (`bundle.ts:115`). Postgres will accept garbage and forms will silently break. **Conclusion: the action layer cannot merely "wrap validated paths" — for several tables it must BE the validation.** This strengthens, not weakens, the case for building it.
3. **Server actions are not externally callable.** They are Next.js `'use server'` functions bound to session-cookie auth; several are thin (property/claims actions do raw upserts with type coercion only). The action layer is therefore an **MCP server living inside the NewChapter repo**, importing `lib/` modules directly and running with the service-role client — the same pattern as the 30 background agents (`lib/agents/admin.ts:8 serviceClient()`, RLS-bypassing, firm-scoped by convention; every tool must self-stamp `firm_id`/`case_id`).
4. **Deliberate departure from the Conductor precedent.** Janet's tool loop (`lib/conductor/agent.ts:59-129`) never writes directly — everything mutating goes through `handoff_write_request` → scribe → `/approvals`. The action layer keeps that pattern for **staff doors**, but Morley's door gets tier-2 direct internal writes (logged, attributable). This is the designed difference between doors, not an accident.
5. **No MCP/tool-API surface exists in NewChapter today** — this layer is new work. Good news: `AgentContext`/`runAgent` (`lib/agents/types.ts:68-106`, `lib/agents/runner.ts:29`) already provide locks, `agent_runs` audit rows, metering, and review gates to plug into for parity.

## 1. Architecture

- **Process:** `mcp/server.ts` in the NewChapter repo (new directory), MCP over stdio for local sessions; later HTTP+bearer for the daemon. Runs with `serviceClient()`; every call carries `actor` (e.g. `firm-agent:morley`) written to an `agent_runs`-style audit row.
- **Validation module:** `mcp/validate.ts` — the enums and formats the DB doesn't enforce: `party_type ∈ {debtor, co_debtor, dependent, spouse}`, SSN 9-digit-or-null, `county_fips` exactly 5 digits, `incurred_by ∈ {debtor1, debtor2, both, other}`, Schedule J line codes from `EXPENSE_LINES` (`expenses/actions.ts`), percent fields normalized once (÷100 exactly one place).
- **Write-back:** every mutating tool appends a one-line entry to `case_notes` (actor, tool, summary) — write-back as architecture.
- **Selfcheck:** `mcp/selfcheck.ts` exercising each tool against a fixture case, in the style of `scripts/efile-selfcheck.ts`; run in CI so migrations break loudly.

## 2. Tool contracts — v0 (Plays 1 + 2 only)

### Reads
| Tool | Backing code | Notes |
|---|---|---|
| `search_cases(query)` | port of Conductor's `search_cases` (`lib/conductor/agent.ts`) | name/case#/chapter/status fragments |
| `get_case_bundle(caseId)` | `fetchCaseBundle` (`lib/forms/bundle.ts:68`) | the one "everything about a case" read; 36 top-level keys |
| `list_documents(caseId, {category?, refresh?})` | `documents` table; `refresh` triggers `syncCaseDocuments` (`lib/sharepoint/documentSync.ts:121`) | categories: pleadings/tax/income/post_petition/correspondence/misc |
| `get_readiness(caseId, {rerun?})` | `pre_filing_readiness` history; `rerun` dispatches `agent.dispatch.pre_filing_check` (`lib/agents/defs/preFilingReadiness.ts`) | deterministic rule-blockers are authoritative over LLM output |

### Compute (pure; every number carries a receipt)
| Tool | Backing code | Notes |
|---|---|---|
| `run_income_generation(caseId, {filingDate?})` | `pay_stubs` rows → `dedupeStubs` + `runIncomeGeneration` (`lib/income/engine.ts:105,351`) | returns `IncomeGeneration` incl. `CalcReceipt` per calc (`lib/income/types.ts:31-39`) |
| `resolve_means_standards(caseId)` | `resolveStandards` (`lib/forms/means-standards.ts:53`) | KNOWN GAP: `vehicleOperating` unseeded → 0; must surface as caveat, never silent |
| `plan_compute(caseId | intake)` | `computeAll` (`lib/plan/compute.ts:44`), floors via `lib/plan/floortests.ts` | returns `Computed` incl. `alerts[]` (`liquidation_cap`, `exceeds_disposable_income`, …) |
| `efile_validate(caseId)` | `validateCaseUpload` (`lib/efile/validate.ts:10`) | blockers + warnings |

### Writes (tier 2 — autonomous, validated, logged)
| Tool | Backing table(s) | Validation owned by action layer |
|---|---|---|
| `save_party(caseId, party)` | `case_parties` | party_type enum (DB won't), SSN format, county_fips 5-digit |
| `save_asset` / `save_exemption` | `assets`, `exemptions` | exemption requires `asset_id`; numeric coercion |
| `save_claim(caseId, kind, claim)` | `secured_claims` / `unsecured_claims` | `incurred_by` enum; 910-day flag consistency |
| `save_expenses(caseId, lines)` | `schedule_j_expenses` | line codes from `EXPENSE_LINES`; whole-set replace semantics made explicit |
| `apply_income_to_schedule_i(caseId, versionId, selections)` | port of `income/actions.ts:147 applyToScheduleI` | the one write with real business logic — upserts `schedule_i_sources`, recomputes `means_test.cmi_*`; refuses NC-migrated versions |
| `save_plan` / `save_treatment` | `ch13_plans`, `ch13_plan_treatments` | percent normalization in exactly one place |
| `add_case_note(caseId, note)` / `append_case_wiki(caseId, md)` | `case_notes`, `cases.wiki_md` | the write-back primitives |
| `create_task` / `create_deadline` | `tasks`, `deadlines` | deadline_type vocabulary from existing rows |

### Documents & filing
| Tool | Backing code | Tier |
|---|---|---|
| `render_form(caseId, formId, {amended?})` | `GET /api/forms/[form]` or direct `renderMergedPdf` | free (archive side-effect on download preserved) |
| `render_forms_bundle(caseId, forms[])` | `/api/forms/bundle`; `applicableForms({chapter, aboveMedian})` picks the set (`registry.ts:75`) | free |
| `generate_efile_package(caseId)` | `buildCaseUploadFile` + `buildCreditorMatrix` (`lib/efile/caseUpload.ts:227,255`) | free to generate; output contains decrypted SSNs — delivered to Morley only, never stored in notes. Transmission respects `TRANSMIT_STATE='held'` (`lib/efile/transmit.ts:16`) — the tool has NO transmit mode in v0 |
| `record_filing(caseId, caseNumber, filedDate)` | sets case fields; NEF parser (`lib/comms/nef.ts:118,212`) auto-captures 341/bar dates from the court's email thereafter | tier 2; triggers deadline scaffolding + arms `pacerTracker`/`341-prep` chain (`lib/agents/events.ts:32`) |

### Meta (flywheel)
`log_friction(entry)` and `create_pitch(pitch)` — new `friction_log` table (or wiki page in v0); pitch handoff to the Archie harness queue is manual in v0.

## 3. Known gaps surfaced by recon (first flywheel entries, pre-seeded)

1. **`case_parties.party_type` needs a real CHECK constraint** + reconciliation of the two debtor2 value sets (`bundle.ts:115` vs. `profile/actions.ts:91` comment). Migration candidate.
2. **No Louisiana exemption-rules module** — statutes/caps are attorney-entered freeform; `buildB106C` hardcodes set + homestead cap (`lib/forms/caseData/b106c.ts:61-63`). Play 1's exemptions pass runs on agent knowledge + firm wiki until an encoded rules table exists. Strong pitch candidate.
3. **`vehicleOperating` standard unseeded** (means-standards) — caveat until UST transportation ingestion lands.
4. **Two parallel exemption CRUD paths** (`lib/actions/exemptions.ts` vs `property/actions.ts`) with no cross-validation — consolidation candidate.
5. **No external auth surface** — v0 uses the service-role key locally (Morley's machine only); a scoped key/JWT is required before any daemon/remote door opens.

## 4. Build sequence

1. `mcp/server.ts` skeleton + `search_cases` + `get_case_bundle` + `add_case_note` (one read, one write, end-to-end proof).
2. Compute four (income, standards, plan, efile-validate) — pure imports, low risk.
3. Play-1 write set + `list_documents` + `get_readiness` → **run Play 1 on the readiest of the 5 unfiled cases.**
4. Forms + efile package tools → **file case #1; harvest frictions; iterate.**
5. Selfcheck + CI wiring; remaining writes hardened as the next 4 filings exercise them.

Candidate execution: this is itself a strong first job for the Archie harness (spec → build → swarm → gate) once Morley approves the spec.
