"""contract.py: the 5-field task contract every A2A hand-off carries.

A hand-off that says "can you look at X" fails in predictable ways: the peer does the wrong thing,
returns the wrong shape, goes out of bounds, never stops, or says "done" with nothing to check.
Anthropic's multi-agent research write-up names exactly these as what a delegated task needs:
an objective, an output format, guidance on tools and sources, and clear task boundaries
(anthropic.com/engineering/multi-agent-research-system). We add a stop condition and the command
that proves it's done (blimp-infra/duke/task-contract-template.md).
"""
from __future__ import annotations

FIELDS = ("objective", "output", "boundaries", "stop_condition", "check_command")
LABELS = {"objective": "Objective", "output": "Output", "boundaries": "Boundaries",
          "stop_condition": "Stop when", "check_command": "Check (run this to prove it's done)"}


def render(**fields: str) -> str:
    """The contract as plain text. Every field is required: a missing one is the usual failure."""
    missing = [f for f in FIELDS if not (fields.get(f) or "").strip()]
    if missing:
        raise ValueError(f"contract is missing: {', '.join(missing)}")
    return "TASK CONTRACT\n" + "\n".join(f"{LABELS[f]}: {fields[f].strip()}" for f in FIELDS)
