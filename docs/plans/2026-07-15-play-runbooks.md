# Firm Agent — Play Runbooks (Draft v0)

**Date:** 2026-07-15
**Companion to:** [design](2026-07-15-firm-agent-design.md) · [action layer v0 spec](2026-07-15-action-layer-v0-spec.md)
Tool names below reference the action-layer spec; where a tool doesn't exist yet, building it during the play IS the plan (build-as-we-file).

---

## Play 1 — Pre-petition audit

**Trigger:** "Audit the ___ file."
**Output:** a readiness memo written to the case wiki + presented in session.

1. **Pull the case.** `get_case_bundle` — full case snapshot. Note chapter, district, filing status, spouse/CP posture.
2. **Document inventory.** List documents on file (documents table / SharePoint). Diff against the required set for chapter + trustee (Ch.13 MDLA/Crawford: DSI, tax affidavit, payroll consent, gambling affidavit; tax returns; pay advices; ID/SSN proof). Output: have / missing / stale.
3. **Income pass.** Run pay stubs through the income engine: dedupe check, CMI (6-full-calendar-months window before filing month), means test vs. median, Schedule I projection. Flag SS/SSD/VA exclusions. Every figure with its receipt. If stubs are missing or stale → friction log + missing-doc item.
4. **Expenses pass.** Schedule J against IRS/local standards; flag lines that draw trustee scrutiny (entertainment ≠ $0 warning, health-care overrides on Line 22, vehicle-commuting justification).
5. **Exemptions pass.** Louisiana rules: homestead (R.S. 20:1, usufruct conflict check), vehicle (R.S. 13:3881(A)(7), one per debtor), cliff exemptions; §522(f) lien-avoidance candidates on judgment liens.
6. **Claims sanity.** Secured claims: 910-day vehicle check, cramdown candidates, intentions set. Unsecured: priority classification, §507(a)(8) tax 3-year window.
7. **Plan pass (Ch.13).** `plan_compute` + floor tests: liquidation floor, disposable-income floor, commitment period, binding floor; feasibility alerts.
8. **Readiness memo.** Green items one line each. Gaps as a checklist with owner (client / staff / Morley). Judgment calls staged with options priced. Write to case wiki; log frictions encountered.

**Gates:** none required — reads, computes, internal writes only. Client document-request emails drafted here are handed to Play-boundary approval.

---

## Play 2 — Case filing

**Trigger:** "File ___" (requires a green — or consciously overridden — Play 1 audit).
**Output:** a filing package in Morley's hands; post-filing scaffolding armed.

1. **Preflight.** Re-run Play 1 deltas since the audit (income re-rolls monthly until filed — recompute if the month turned). Confirm all blocking gaps closed or explicitly waived by Morley (logged).
2. **Forms bundle.** Render the full form set for chapter via the forms API; visual-spot-check pass (agent reads rendered output for empty required fields, misrouted lines — the gap-analysis failure classes).
3. **CM/ECF package.** Generate Debtor.txt + creditor matrix via the efile module; run the efile validation + selfcheck. Transmission stays HELD — output is a download for manual CM/ECF upload (court constraint, not a gate we control).
4. **Hand-off.** Package + a one-page filing checklist to Morley. He files at ECF. **This step is his by design.**
5. **Post-filing capture.** On his "filed, case number ___": record case number + filing date, capture 341 date (docket sweep or manual), spawn the deadline set (341, objection bars, plan/confirmation dates per district) and pre-341 task list; arm the docket/PACER watch for the new case.
6. **Learn.** This play is built live against the 5 pending cases — after each filing, harvest the friction log into pitches/spec candidates before starting the next.

**Gates:** the ECF upload itself is manual-by-Morley; any client/trustee emails drafted along the way are gated.

---

## Play 3 — Hearing/341 briefing

**Trigger:** "Prep Thursday" / standing pre-hearing cadence (ambient in Phase 2).
**Output:** a morning-of briefing memo per case + chase actions staged.

1. Pull the hearing list for the date (hearings/docket) and classify: 341s, confirmation hearings, motions.
2. Per 341: trustee-requirements diff (documents on file vs. Crawford's pre-341 set), payment posture (NDC/delinquency), open POC anomalies, prior-continuance history.
3. Per confirmation/motion: current plan vs. objections on file, drift flags, feasibility now (re-run floor tests with latest data).
4. Missing anything a client must supply → draft the chase email/text (gated), attach to the approvals queue with the hearing date as urgency.
5. **Briefing memo per case:** posture in three sentences, money numbers with receipts, the open risk, the one thing not to forget. Written to case wiki + assembled into a single docket-day brief.

**Gates:** all client/trustee outbound gated; internal notes/tasks free.

---

## Play 4 — Confirmation work

**Trigger:** "Get ___ to confirmation."
**Output:** the amended-paper set drafted for Morley's edit; objection strategy staged.

1. **One picture.** Objections filed (trustee + creditors), plan-drift flags, POC register vs. schedules reconciliation, current feasibility.
2. **Strategy staging.** For each objection: cure options priced (amend plan / amend schedules / respond / negotiate), with plan-math consequences computed for each path.
3. **Drafting.** On Morley's direction: amended plan via the plan module (LAMB form), amended schedules via the forms pipeline, responses/objections to POCs via the objection engine (prescription, standing, 3001(c), amount variance, late-filed, post-petition interest grounds), certificates/notices per template set.
4. **Filing prep.** Amended-paper package assembled as in Play 2 step 3-4; Morley files.
5. **Post-ruling capture.** Outcome recorded to case record; recurring objection patterns from this trustee/creditor mined into the friction log (the trusteePatternMiner agent already exists as a sensor here).

**Gates:** all outbound gated; drafts are internal until Morley's go.

---

## Cross-play rules

- Every play ends by writing its product to the case record (wiki/notes) — the session is not the system of record.
- Every play harvests its friction log before closing.
- Any number shown to Morley carries a receipt.
- A play that cannot complete honestly says exactly where it stopped and why — no fake-green.
