# CLAUDE.md — archie (build harness + Archie's domains)

This repo is the **build harness** (spec → segmented build with council checkpoints →
swarm review → two-step gated merge) plus Archie's own domains (antiques pipeline,
personal assistant). Per-subsystem docs: `swarm/README.md`, `port/PORT_PLAN.md`,
`skills/harness-control/SKILL.md`, `antiques/README.md`.

## Cross-session continuity (work-assistant protocol, 2026-07-16)

Work happens from multiple machines and sessions. Before starting: **pull
`dimemc24-hash/work-assistant` and read `state/NOW.md` + `state/decisions.md`**
(binding decisions live there) and check this repo's origin both directions before
modifying shared files. Before stopping: push every branch you touched (WIP included)
and update work-assistant's `state/`. Git is reality; local is scratch.

The firm work assistant lives in `dimemc24-hash/work-assistant` (three-actor split:
that repo = the agent; `newchapter` = app + action layer; this repo = the harness).
Harness build bundles for the assistant are authored THERE under `_harness/` and
target this harness via profiles (`profiles/`, `swarm/README.md`).

Note: `archie-old/` (untracked) is the 2025 legacy app — reference only, contains
stale secrets pending rotation; never build on it.
