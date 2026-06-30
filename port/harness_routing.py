#!/usr/bin/env python3
"""
harness_routing.py — segment model escalation for the Python dev-harness.

Faithful port of archie-router/src/router/escalation.ts `decide()`, collapsed
onto the three-tier model ladder the harness already uses (cheap / standard /
premium). The router's full role/panel/depth machinery is reduced to the
single decision the harness needs: given a base (default) model and four
escalation signals, pick the tier and return a concrete model slug.

Precedence (port of escalation.ts lines 128-143, triggers block):
    red_zone  >  retry  >  blast_radius  >  stuck  >  default

Safety invariants ported from escalation.ts (Critic #1 hardening):
  - Malformed numeric inputs fail CLOSED (raise), never emit an undefined model.
  - red_zone is a hard safety floor: when True it wins the trigger tie-break
    even if retry/blast/stuck are also true (escalation.ts line 134 ordering).
  - The function NEVER returns None. Every code path either returns a concrete
    model slug or raises.

The tier ladder mirrors REVIEW_harness.md §3.1's recommendation:
    MODEL_TIERS = {"cheap": ..., "standard": ..., "premium": ...}
escalating up the ladder, de-escalating back to base when nothing fires.

This module is self-contained: no archie-router imports, no I/O, no globals
beyond the constant ladder. It is imported by stage2_build.py's core_loop.
"""
from __future__ import annotations

import math
from typing import Optional


# ── tier ladder (REVIEW_harness.md §3.1) ────────────────────────────────────
# The harness's three cost tiers. `pick_segment_model` returns one of these
# slugs; escalation climbs cheap → standard → premium. The base_model argument
# selects which tier the run starts on (Opus for real builds, glm-5.2 for smoke).
MODEL_TIERS = {
    "cheap":    "z-ai/glm-5.2",
    "standard": "anthropic/claude-sonnet-4.6",
    "premium":  "anthropic/claude-opus-4.8",
}

# Ordered ladder (index 0 = cheapest). Escalation moves right; the default
# branch returns the base tier (index of base_model's tier).
_TIER_ORDER = ["cheap", "standard", "premium"]


class RoutingError(ValueError):
    """Raised on malformed caller-supplied inputs (fail-closed)."""


def _assert_int_at_least(name: str, v: object, minimum: int) -> int:
    """Port of escalation.ts assertIntAtLeast — fail closed on bad indices.

    Rejects None, bools (bool is an int subclass in Python — guard explicitly),
    non-integers, floats, NaN/Infinity, and values below `minimum`.
    """
    if isinstance(v, bool) or not isinstance(v, int) and not isinstance(v, float):
        raise RoutingError(f"{name} must be an integer >= {minimum} (got {v!r})")
    if isinstance(v, float):
        if not math.isfinite(v) or v != int(v):
            raise RoutingError(f"{name} must be an integer >= {minimum} (got {v!r})")
        v = int(v)
    if not isinstance(v, int) or v < minimum:
        raise RoutingError(f"{name} must be an integer >= {minimum} (got {v!r})")
    return v


def _tier_of(model: str) -> int:
    """Index into _TIER_ORDER for a model slug. Falls back to 'cheap' (index 0)
    for unknown models — the safe (cheap) direction, mirroring escalation.ts's
    'unknown model → 0' ledger convention for price lookup."""
    for i, tier in enumerate(_TIER_ORDER):
        if MODEL_TIERS[tier] == model:
            return i
    return 0


def pick_segment_model(
    base_model: str,
    *,
    red_zone: bool = False,
    retries: int = 0,
    blast: int = 0,
    stuck: bool = False,
) -> str:
    """Pick the segment model slug, porting escalation.ts `decide()` triggers.

    Parameters mirror the four escalation signals the harness loop already has
    in scope (REVIEW_harness.md §3.1):
      - base_model : the run's default tier model (BUILD_MODEL or smoke default)
      - red_zone   : detect_red_zone() result for the changed-file set
      - retries    : nudge counter (escalation.ts `attempt - 1`)
      - blast      : number of changed files (escalation.ts `files.length`)
      - stuck      : the nudge-branch / no-sentinel signal

    Returns a concrete model slug from MODEL_TIERS. NEVER returns None.

    Precedence (escalation.ts lines 133-137):
        redZone > retry > blastRadius > stuck > none
    Each trigger escalates exactly one tier above the base (never beyond
    'premium'), matching the router's single-step escalation model. If a
    trigger fires and the base is already 'premium', it stays premium.

    Thresholds ported from router.config.json:
      - escalateAfterRetries = 2  → retry fires when retries > 2
      - blastRadiusThreshold = 3  → blast fires when blast >= 3
    """
    if not isinstance(base_model, str) or not base_model:
        raise RoutingError(f"base_model must be a non-empty string (got {base_model!r})")

    # Fail-closed validation (escalation.ts assertIntAtLeast, lines 49-53, 64).
    # retries maps to attempt-1; it must be a non-negative integer.
    retries = _assert_int_at_least("retries", retries, 0)
    # blast (files.length) is a count — non-negative integer.
    blast = _assert_int_at_least("blast", blast, 0)
    # red_zone / stuck are booleans; coerce defensively but reject non-bool.
    if not isinstance(red_zone, bool):
        raise RoutingError(f"red_zone must be a bool (got {red_zone!r})")
    if not isinstance(stuck, bool):
        raise RoutingError(f"stuck must be a bool (got {stuck!r})")

    base_idx = _tier_of(base_model)

    # Triggers (escalation.ts lines 129-131), in evaluation order.
    # NOTE: escalation.ts evaluates them as independent booleans, then selects
    # the trigger via the precedence chain (lines 134-137). We collapse the
    # two steps: determine the winning trigger, then escalate exactly one tier.
    # retries maps to attempt-1 (attempt is 1-based in the router); the router's
    # `attempt > escalateAfterRetries` (escalateAfterRetries=2) is therefore
    # `retries+1 > 2` i.e. `retries > 1`.
    retry = retries > 1           # attempt > escalateAfterRetries (attempt = retries+1)
    blast_hit = blast >= 3        # files.length >= blastRadiusThreshold

    # Precedence: red_zone > retry > blast > stuck > none.
    if red_zone:
        trigger = "redZone"
    elif retry:
        trigger = "retry"
    elif blast_hit:
        trigger = "blastRadius"
    elif stuck:
        trigger = "stuck"
    else:
        trigger = "none"

    if trigger == "none":
        return base_model

    # Escalate exactly one tier up the ladder, clamped at 'premium'.
    # (The router escalates to the role's escalation model; the harness's
    # flat ladder equivalent is one tier above base, capped at premium.)
    escalated_idx = min(base_idx + 1, len(_TIER_ORDER) - 1)
    return MODEL_TIERS[_TIER_ORDER[escalated_idx]]


def trigger_for(
    *,
    red_zone: bool = False,
    retries: int = 0,
    blast: int = 0,
    stuck: bool = False,
) -> str:
    """Return the winning trigger name without picking a model.

    Exposed for the audit log (cklog in stage2_build.py core_loop) so the
    checkpoint record carries WHY the model escalated, mirroring the router's
    RouteDecision.trigger field. Same precedence as pick_segment_model.
    """
    retries = _assert_int_at_least("retries", retries, 0)
    blast = _assert_int_at_least("blast", blast, 0)
    if not isinstance(red_zone, bool):
        raise RoutingError(f"red_zone must be a bool (got {red_zone!r})")
    if not isinstance(stuck, bool):
        raise RoutingError(f"stuck must be a bool (got {stuck!r})")

    if red_zone:
        return "redZone"
    if retries > 1:
        return "retry"
    if blast >= 3:
        return "blastRadius"
    if stuck:
        return "stuck"
    return "none"


__all__ = ["pick_segment_model", "trigger_for", "MODEL_TIERS", "RoutingError"]
