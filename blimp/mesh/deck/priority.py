"""Blimp Crew v2 — deterministic priority scorer (spec §9.6).

LLMs propose inputs; this code computes the score. Zero LLM.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

VALID_SCORES = {1, 2, 3, 5, 8}
REVENUE_LINK = {0, 3, 8}


def priority_score(
    value: int,
    urgency: int,
    risk_reduction: int,
    revenue_link: int,
    effort: int,
    pinned: bool = False,
    incident: bool = False,
    due_at: str | None = None,
    now: datetime | None = None,
) -> float:
    """priority = (value + urgency + risk_reduction + revenue_link) / max(effort, 1)

    Overrides: the Boss pin (+inf), incident (+inf), due in < 48h (x1.5).
    Raises ValueError on out-of-vocabulary inputs (LLMs propose; code validates).
    """
    for name, v in (("value", value), ("urgency", urgency), ("risk_reduction", risk_reduction)):
        if v not in VALID_SCORES:
            raise ValueError(f"{name}={v} not in {sorted(VALID_SCORES)}")
    if revenue_link not in REVENUE_LINK:
        raise ValueError(f"revenue_link={revenue_link} not in {sorted(REVENUE_LINK)}")
    if effort not in VALID_SCORES:
        raise ValueError(f"effort={effort} not in {sorted(VALID_SCORES)}")

    if pinned:
        return float("inf")
    if incident:
        return float("inf")

    base = (value + urgency + risk_reduction + revenue_link) / max(effort, 1)

    if due_at:
        now = now or datetime.now(timezone.utc)
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        if due - now < timedelta(hours=48):
            base *= 1.5
    return round(base, 3)


def sort_ready_tasks(tasks: list[dict]) -> list[dict]:
    """Sort READY tasks by score desc, then age (created_at) asc."""
    return sorted(
        tasks,
        key=lambda t: (-t["priority"], t.get("created_at", "")),
    )
