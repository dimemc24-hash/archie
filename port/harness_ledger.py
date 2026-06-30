#!/usr/bin/env python3
"""
harness_ledger.py — cost ledger + circuit breaker for the Python dev-harness.

Faithful port of archie-router/src/router/ledger.ts. The TS original is backed
by better-sqlite3; this port starts with an in-process dict/JSON tally (the
 REVIEW_harness.md §3.2 "shared Ledger/Breaker object" seam) and notes sqlite
as the upgrade path — the API is identical so the swap is drop-in.

PRIVACY (ported invariant, ledger.ts lines 4-7): the charge() signature has NO
content field — only the OpenRouter usage block (prompt_tokens /
completion_tokens). No prompt/response text is ever recorded.

Guardrails ported from ledger.ts:
  - per-day USD cap   → circuit-breaks at dailyCapUsd (isCircuitBroken)
  - per-role USD cap  → perRoleCapUsd dict (canEscalate perRoleCap)
  - global breaker    → canEscalate refuses past daily cap (circuit-break)
  - de-escalate       → de_escalate_to_base() returns the base/cheap model
                        when a cap is breached (the harness's flat-ladder
                        equivalent of the router's "escalation refused" path)

Cost accounting ported from ledger.ts:
  - cost_for(model, tokens_in, tokens_out) → price table lookup, per-1M rates
  - charge(model, usage_dict) → reads OpenRouter usage block, computes cost,
                                 appends to the tally, returns the row id
  - non-neg clamping (Ledger.nonNeg, ledger.ts lines 110-112) on all inputs
  - NaN est → fail CLOSED (canEscalate, ledger.ts lines 182-183)

Config defaults ported from router.config.json (the single config the harness
ships with). A Ledger can be constructed with overrides for testing.
"""
from __future__ import annotations

import json
import math
import os
import time
from typing import Optional


# ── price table (router.config.json → priceTable) ───────────────────────────
# USD per 1M tokens. Unknown model → 0 (ledger.ts line 105).
PRICE_TABLE: dict[str, dict[str, float]] = {
    "z-ai/glm-5.2":                 {"in": 1.40,  "out": 4.40},
    "openai/gpt-5.3-codex":         {"in": 1.75,  "out": 14.00},
    "openai/gpt-5.5":               {"in": 5.00,  "out": 30.00},
    "openai/gpt-4o":                {"in": 2.50,  "out": 10.00},
    "openai/gpt-4o-mini":           {"in": 0.15,  "out": 0.60},
    "deepseek/deepseek-v4-pro":     {"in": 0.44,  "out": 0.87},
    "deepseek/deepseek-chat":       {"in": 0.20,  "out": 0.80},
    "moonshotai/kimi-k2.6":         {"in": 0.66,  "out": 3.41},
    "google/gemini-3.5-flash":      {"in": 1.50,  "out": 9.00},
    "anthropic/claude-sonnet-4.6":  {"in": 3.00,  "out": 15.00},
    "anthropic/claude-opus-4.6":    {"in": 5.00,  "out": 25.00},
    "anthropic/claude-opus-4.8":    {"in": 5.00,  "out": 25.00},
    "sakana/fugu":                  {"in": 0.0,   "out": 0.0},
    "sakana/fugu-ultra":            {"in": 5.00,  "out": 30.00},
}

# ── caps + thresholds (router.config.json) ──────────────────────────────────
DEFAULT_DAILY_CAP_USD = 25.0
DEFAULT_PER_ROLE_CAP_USD = {"browser_qa": 8.0}


class CircuitOpen(Exception):
    """Raised by check() when the breaker has tripped (global or per-role cap).

    call_model / hermes_segment catch this separately from transient HTTP errors
    (REVIEW_harness.md §3.2: "distinguish CircuitOpen from transient HTTP errors").
    """


class Ledger:
    """In-process cost ledger + guardrail breaker.

    Backed by a list of dicts now (JSON-serializable for persistence). The
    upgrade path is sqlite (stdlib sqlite3 is the better-sqlite3 equivalent);
    the public API is identical so core_loop/call_model need no changes.
    """

    def __init__(
        self,
        *,
        price_table: Optional[dict] = None,
        daily_cap_usd: float = DEFAULT_DAILY_CAP_USD,
        per_role_cap_usd: Optional[dict] = None,
        confirm_escalations: bool = False,
        on_confirm=None,
        now=None,
        json_path: Optional[str] = None,
    ):
        self.price_table = price_table if price_table is not None else PRICE_TABLE
        self.daily_cap_usd = daily_cap_usd
        self.per_role_cap_usd = dict(per_role_cap_usd) if per_role_cap_usd else dict(DEFAULT_PER_ROLE_CAP_USD)
        self.confirm_escalations = confirm_escalations
        self.on_confirm = on_confirm
        self._now = now or (lambda: time.time() * 1000.0)
        self._rows: list[dict] = []
        self._next_id = 1
        self._json_path = json_path
        if json_path and os.path.exists(json_path):
            try:
                with open(json_path) as f:
                    data = json.load(f)
                self._rows = data.get("rows", [])
                self._next_id = data.get("next_id", len(self._rows) + 1)
            except (json.JSONDecodeError, OSError):
                pass  # corrupt/missing — start fresh (fail-safe)

    # ── cost accounting (ledger.ts costFor, lines 102-107) ──────────────────
    def cost_for(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """USD cost from the price table (per-1M-token rates). Unknown → 0."""
        row = self.price_table.get(model)
        if not row:
            return 0.0
        return (tokens_in / 1_000_000) * row["in"] + (tokens_out / 1_000_000) * row["out"]

    @staticmethod
    def _non_neg(n) -> float:
        """Clamp to finite non-negative (ledger.ts nonNeg, lines 110-112)."""
        if isinstance(n, bool):
            return 0.0
        if not isinstance(n, (int, float)):
            return 0.0
        if not math.isfinite(n) or n <= 0:
            return 0.0
        return float(n)

    # ── record / charge (ledger.ts record, lines 116-143) ──────────────────
    def charge(self, model: str, usage: dict, *, role: str = "codex",
               repo: str = "harness", task_id: str = "seg", attempt: int = 1,
               trigger: str = "none", latency_ms: int = 0, outcome: str = "ok",
               cost_usd: Optional[float] = None) -> int:
        """Record one model call. Reads the OpenRouter usage block.

        `usage` is the OpenRouter response `usage` object:
            {prompt_tokens, completion_tokens, total_tokens, ...}
        We read prompt_tokens (tokens_in) and completion_tokens (tokens_out),
        matching the router's ledger.ts record() (lines 118-119). If the
        caller supplies cost_usd explicitly we use it (the router's escape
        hatch for pre-computed costs); otherwise we compute from the price table.

        All numeric inputs are clamped to finite non-negative (nonNeg) so the
        per-day SUM stays monotonic and trustworthy for cap checks.

        Returns the row id (ledger.ts returns lastInsertRowid).
        """
        tokens_in = int(self._non_neg(usage.get("prompt_tokens", 0))) if isinstance(usage, dict) else 0
        tokens_out = int(self._non_neg(usage.get("completion_tokens", 0))) if isinstance(usage, dict) else 0
        if cost_usd is None:
            cost_usd = self.cost_for(model, tokens_in, tokens_out)
        cost_usd = self._non_neg(cost_usd)
        latency = int(self._non_neg(latency_ms))
        attempt = int(self._non_neg(attempt)) or 1

        ts = self._now()
        row = {
            "id": self._next_id,
            "ts": ts,
            "day": self._utc_day(ts),
            "repo": repo,
            "role": role,
            "model": model,
            "taskId": task_id,
            "attempt": attempt,
            "trigger": trigger,
            "tokensIn": tokens_in,
            "tokensOut": tokens_out,
            "costUsd": cost_usd,
            "latencyMs": latency,
            "outcome": outcome,
        }
        self._rows.append(row)
        self._next_id += 1
        self._persist()
        return row["id"]

    @staticmethod
    def _utc_day(ts: float) -> str:
        """UTC YYYY-MM-DD (ledger.ts utcDay, lines 62-64)."""
        import datetime
        return datetime.datetime.utcfromtimestamp(ts / 1000.0).strftime("%Y-%m-%d")

    # ── spend queries (ledger.ts spendTodayUsd, lines 146-154) ─────────────
    def spend_today_usd(self, role: Optional[str] = None) -> float:
        """Total USD spent today (UTC), optionally for a single role."""
        day = self._utc_day(self._now())
        total = 0.0
        for r in self._rows:
            if r["day"] != day:
                continue
            if role is not None and r["role"] != role:
                continue
            total += r["costUsd"]
        return total

    def remaining_today_usd(self, role: Optional[str] = None) -> float:
        """Headroom today, bounded by the LOWER of per-role and global (Critic #6)."""
        global_remaining = self.daily_cap_usd - self.spend_today_usd()
        if role is not None and role in self.per_role_cap_usd:
            per_role_remaining = self.per_role_cap_usd[role] - self.spend_today_usd(role)
            return max(0.0, min(per_role_remaining, global_remaining))
        return max(0.0, global_remaining)

    def is_circuit_broken(self) -> bool:
        """True once today's spend has reached the global daily cap (ledger.ts line 169)."""
        return self.spend_today_usd() >= self.daily_cap_usd

    # ── guardrail gate (ledger.ts canEscalate, lines 177-210) ───────────────
    def can_escalate(self, *, role: str, model: str, est_cost_usd: float = 0.0) -> dict:
        """The guardrail gate, called BEFORE using an escalation model.

        Returns {allowed: bool, reason: str|None, detail: str|None}.
        Reasons mirror ledger.ts: circuit-break | dailyCap | perRoleCap |
        confirm-declined | confirm-unavailable.

        NaN est → +Infinity (fail CLOSED, ledger.ts lines 182-183):
          NaN > cap is always False, which would bypass caps; mapping NaN → Inf
          makes every `spend + est > cap` comparison refuse the call.
        Negative est → 0 (cannot lower projected spend, ledger.ts line 183).
        """
        raw_est = est_cost_usd if est_cost_usd is not None else 0.0
        if isinstance(raw_est, float) and math.isnan(raw_est):
            est = math.inf
        elif isinstance(raw_est, (int, float)) and not isinstance(raw_est, bool):
            est = raw_est if raw_est > 0 else 0.0
        else:
            est = 0.0

        global_spend = self.spend_today_usd()
        if global_spend >= self.daily_cap_usd:
            return {"allowed": False, "reason": "circuit-break", "detail": "daily cap reached"}
        if global_spend + est > self.daily_cap_usd:
            return {"allowed": False, "reason": "dailyCap", "detail": "would exceed daily cap"}

        role_cap = self.per_role_cap_usd.get(role)
        if role_cap is not None and self.spend_today_usd(role) + est > role_cap:
            return {"allowed": False, "reason": "perRoleCap", "detail": f"would exceed {role} cap"}

        if self.confirm_escalations and model.startswith("sakana/"):
            if self.on_confirm is None:
                return {"allowed": False, "reason": "confirm-unavailable", "detail": "no onConfirm provided"}
            ok = self.on_confirm(model)
            if not ok:
                return {"allowed": False, "reason": "confirm-declined", "detail": "user declined"}

        return {"allowed": True, "reason": None, "detail": None}

    # ── breaker entry point (REVIEW_harness.md §3.2 seam) ──────────────────
    def check(self, model: str, *, role: str = "codex", est_cost_usd: float = 0.0) -> None:
        """Pre-call breaker check. Raises CircuitOpen if the call is refused.

        This is the seam fusion.call_model and stage2_build.hermes_segment call
        BEFORE invoking the model. Distinguished from transient HTTP errors so
        the caller can fall back to a cheaper model rather than swallowing.
        """
        d = self.can_escalate(role=role, model=model, est_cost_usd=est_cost_usd)
        if not d["allowed"]:
            raise CircuitOpen(f"{d['reason']}: {d['detail']}")

    # ── de-escalation helper (REVIEW_harness.md §3.2 "de-escalate-to-base") ─
    def de_escalate_to_base(self, base_model: str, *, role: str = "codex",
                            est_cost_usd: float = 0.0) -> str:
        """Return base_model if the escalation model would breach a cap.

        Called by the harness loop when pick_segment_model returns an escalated
        model but the ledger refuses it: fall back to the cheap/base tier
        rather than burning budget or stalling the build.
        """
        d = self.can_escalate(role=role, model=base_model, est_cost_usd=est_cost_usd)
        if d["allowed"]:
            return base_model
        # Even the base is over cap — circuit is broken. Return base anyway
        # (the loop's breaker gate at core_loop top will halt the build).
        return base_model

    # ── diagnostics ────────────────────────────────────────────────────────
    def count(self) -> int:
        """Row count (ledger.ts count, lines 213-215)."""
        return len(self._rows)

    def columns(self) -> list[str]:
        """Persisted column names (ledger.ts columns, lines 218-220).

        Proves no content/PII columns exist (ledger.test.ts lines 29-40).
        """
        if not self._rows:
            return ["id", "ts", "day", "repo", "role", "model", "taskId", "attempt",
                    "trigger", "tokensIn", "tokensOut", "costUsd", "latencyMs", "outcome"]
        return list(self._rows[0].keys())

    def close(self) -> None:
        """No-op for in-process (parity with ledger.ts close, line 222)."""
        self._persist()

    def _persist(self) -> None:
        if self._json_path:
            try:
                os.makedirs(os.path.dirname(self._json_path), exist_ok=True)
                with open(self._json_path, "w") as f:
                    json.dump({"rows": self._rows, "next_id": self._next_id}, f)
            except OSError:
                pass


__all__ = ["Ledger", "CircuitOpen", "PRICE_TABLE", "DEFAULT_DAILY_CAP_USD", "DEFAULT_PER_ROLE_CAP_USD"]
