"""Blimp Crew v2 — Ballast: deterministic budget governor and loop breaker (spec §14.5, §9.8).

Zero LLM. Money math is code over policy files, never a model (L-024).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BudgetLimits:
    global_monthly_usd: float
    split: dict[str, float]                      # duke: 0.40, hermes: 0.25, ...
    per_turn: dict[str, int]                     # max_input_tokens, max_output_tokens, max_tool_calls, max_wall_clock_s
    per_task_by_effort: dict[str, dict[str, int]]  # S/M/L -> tokens/turns
    huddle: dict[str, int]                       # max_rounds, max_tokens
    thresholds: dict[str, float]                 # warn, hard_stop
    loop_breaker: dict[str, int]                 # repeats, unchanged_ledger_turns, error_streak
    kill_switch_spike_multiplier: float = 3.0

    @classmethod
    def from_policy(cls, policy: dict[str, Any]) -> "BudgetLimits":
        return cls(
            global_monthly_usd=float(policy["global_monthly_usd"]),
            split=policy["split"],
            per_turn=policy["per_turn"],
            per_task_by_effort=policy["per_task_by_effort"],
            huddle=policy["huddle"],
            thresholds=policy["thresholds"],
            loop_breaker=policy["loop_breaker"],
            kill_switch_spike_multiplier=policy.get("kill_switch", {}).get(
                "daily_spend_spike_multiplier", 3.0
            ),
        )


@dataclass
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    wall_clock_s: int = 0
    cache_read_tokens: int = 0


@dataclass
class TaskSpendTracker:
    """Loop-breaker state machine, computed by Ballast — never by the agent (spec §9.8)."""
    tool_call_digests: list[str] = field(default_factory=list)
    ledger_versions: list[int] = field(default_factory=list)
    error_streak: int = 0
    looping: bool = False


def check_turn_limits(limits: dict[str, int], usage: TurnUsage) -> str | None:
    """Return the first exceeded limit name, or None. Called by the host each tool round."""
    if usage.input_tokens > limits["max_input_tokens"]:
        return "max_input_tokens"
    if usage.output_tokens > limits["max_output_tokens"]:
        return "max_output_tokens"
    if usage.tool_calls > limits["max_tool_calls"]:
        return "max_tool_calls"
    if usage.wall_clock_s > limits["max_wall_clock_s"]:
        return "max_wall_clock_s"
    return None


def task_budget_check(
    effort_class: str,
    per_task_by_effort: dict[str, dict[str, int]],
    spent_tokens: int,
    spent_turns: int,
    thresholds: dict[str, float],
    is_complete: bool,
) -> str:
    """Return one of: 'ok', 'warn', 'hard_stop', 'replan' (80% without progress)."""
    cap = per_task_by_effort[effort_class]
    token_frac = spent_tokens / max(cap["tokens"], 1)
    turn_frac = spent_turns / max(cap["turns"], 1)
    frac = max(token_frac, turn_frac)
    if frac >= thresholds["hard_stop"]:
        return "hard_stop"
    if token_frac >= thresholds["warn"] and not is_complete:
        return "replan"
    if frac >= thresholds["warn"]:
        return "warn"
    return "ok"


def loop_breaker_check(tracker: TaskSpendTracker, policy: dict[str, int]) -> str | None:
    """Deterministic loop detection. Sets tracker.looping; returns an action or None."""
    lb = policy
    # identical tool call + args digest repeated within recent window
    if len(tracker.tool_call_digests) >= lb["identical_tool_call_repeats"]:
        tail = tracker.tool_call_digests[-lb["identical_tool_call_repeats"]:]
        if len(set(tail)) == 1:
            tracker.looping = True
            return "pause_task_and_alert_frank"
    # ledger unchanged across N turns
    if len(tracker.ledger_versions) >= lb["unchanged_ledger_turns"]:
        tail = tracker.ledger_versions[-lb["unchanged_ledger_turns"]:]
        if len(set(tail)) == 1:
            tracker.looping = True
            return "pause_task_and_alert_frank"
    # error streak
    if tracker.error_streak >= lb["error_streak"]:
        tracker.looping = True
        return "pause_task_and_alert_frank"
    return None


def digest_tool_call(tool: str, args: dict[str, Any]) -> str:
    """Args digest for receipt + loop detection: sha256 over canonical json."""
    canonical = json.dumps({"tool": tool, "args": args}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def kill_switch_check(daily_spend_usd: float, trailing_7d_median_usd: float, multiplier: float) -> bool:
    """Spend spike -> auto-freeze + sev2 (spec §14.5 kill_switch)."""
    if trailing_7d_median_usd <= 0:
        return False
    return daily_spend_usd >= multiplier * trailing_7d_median_usd
