"""Blimp Crew v2 — Message envelope validation (spec §8.3, §16.1) + anti-chatter lint (§8.11).

Deterministic. Deck rejects with a 400-class A2A error when any validation fails.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

VALID_ACTS = {
    "REQUEST", "QUERY", "ANSWER", "PROPOSE", "ACCEPT", "COUNTER", "REJECT",
    "PUSHBACK", "HANDOFF", "STATUS", "DONE", "ALERT", "INFORM", "ESCALATE",
}
VALID_EXPECTS = {"none", "answer", "decision", "accept", "ack"}
VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
VALID_VISIBILITY = {"thread", "hq", "private"}

MAX_BODY_TOKENS = 1500
MAX_PURPOSE_CHARS = 160

# crude token estimate: words + punctuation clusters; deliberately conservative
def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3.8))

# A first line that is ONLY an acknowledgement ("Got it.", "thanks!", "on it 👍") is chatter.
# A real message that merely starts with one ("OK, two options: ...") is not.
ACK_PATTERNS = re.compile(
    r"^\s*(got it|thanks|thanks a lot|thank you|ty|on it|will do|ack|acknowledged|noted|ok|okay|"
    r"sure|roger|sounds good|cool|great|perfect|understood|copy that)"
    r"(\s*(,|and|&)?\s*(got it|thanks|thank you|on it|will do|noted|sounds good))*"
    r"[\s!.,:;)\]\-~]*([\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]\s*)*$",
    re.IGNORECASE,
)
# Gratitude to the Boss is allowed with an explicit flag (spec 8.4 override).
GRATITUDE_FLAG = "gratitude_to_logan"

WAKES_BY_ACT = {
    "REQUEST": True, "QUERY": True, "ANSWER": True, "PROPOSE": True,
    "ACCEPT": True, "COUNTER": True, "REJECT": True, "PUSHBACK": True,
    "HANDOFF": True, "STATUS": False, "DONE": False, "ALERT": True,
    "INFORM": False, "ESCALATE": False,  # ESCALATE wakes the decision queue, not an agent
}


def validate_envelope(
    msg: dict[str, Any],
    *,
    known_agents: set[str],
    blocked_pairs: set[tuple[str, str]] | None = None,
    sender_budget_remaining_tokens: int | None = None,
    now: datetime | None = None,
) -> tuple[bool, list[str]]:
    """Validate an A2A Message + Blimp speech-act extension. Returns (ok, errors[])."""
    errors: list[str] = []
    blocked_pairs = blocked_pairs or set()
    now = now or datetime.now(timezone.utc)

    sa = (msg.get("metadata") or {}).get("https://blimp.my/a2a/ext/speech-act/v1") or {}
    act = sa.get("act")
    sender = sa.get("from")
    to = sa.get("to") or []
    expects = sa.get("expects", "none")
    priority = sa.get("priority", "P2")
    visibility = sa.get("visibility", "thread")
    purpose = sa.get("purpose", "")
    reply_by = sa.get("reply_by")

    # --- envelope basics -------------------------------------------------------
    if not msg.get("messageId"):
        errors.append("messageId required (ULID)")
    if not msg.get("contextId"):
        errors.append("contextId required")
    if not msg.get("taskId"):
        errors.append("taskId required — always attach the task")
    if act not in VALID_ACTS:
        errors.append(f"act '{act}' invalid")
    if expects not in VALID_EXPECTS:
        errors.append(f"expects '{expects}' invalid")
    if priority not in VALID_PRIORITIES:
        errors.append(f"priority '{priority}' invalid")
    if visibility not in VALID_VISIBILITY:
        errors.append(f"visibility '{visibility}' invalid")

    # --- routing ----------------------------------------------------------------
    if not to:
        errors.append("to[] required (minItems 1)")
    for r in to:
        if r not in known_agents:
            errors.append(f"recipient '{r}' does not resolve")
    for pair in [(sender, r) for r in to]:
        if pair in blocked_pairs:
            errors.append(f"recipient '{pair[1]}' has blocked sender '{pair[0]}'")

    # --- body size ----------------------------------------------------------------
    text_parts = " ".join(
        p.get("text", "") for p in (msg.get("parts") or []) if isinstance(p, dict)
    )
    est = estimate_tokens(text_parts)
    if est > MAX_BODY_TOKENS:
        errors.append(f"body ~{est} tokens > {MAX_BODY_TOKENS}; put it in an artifact and send the reference")

    # --- purpose + reply_by -------------------------------------------------------
    if len(purpose) > MAX_PURPOSE_CHARS:
        errors.append(f"purpose {len(purpose)} chars > {MAX_PURPOSE_CHARS}")
    if expects != "none" and not reply_by:
        errors.append(f"expects={expects} requires reply_by")
    if reply_by:
        try:
            rb = datetime.fromisoformat(str(reply_by).replace("Z", "+00:00"))
            if rb <= now:
                errors.append("reply_by is in the past")
        except ValueError:
            errors.append("reply_by not ISO-8601")

    # --- budget --------------------------------------------------------------------
    if sender_budget_remaining_tokens is not None and est > sender_budget_remaining_tokens:
        errors.append("sender budget exhausted")

    # --- anti-chatter (§8.11) -------------------------------------------------------
    first_line = text_parts.strip().split("\n")[0] if text_parts.strip() else ""
    grateful = bool(sa.get(GRATITUDE_FLAG)) and "logan" in to
    if act not in ("DONE", "ALERT") and first_line and ACK_PATTERNS.match(first_line) and not grateful:
        errors.append("acknowledgement messages are rejected (delivery/reads are tracked)")
    if sa.get(GRATITUDE_FLAG) and "logan" not in to:
        errors.append(f"{GRATITUDE_FLAG} is only valid on messages to logan")

    # handoff must carry the packet
    if act == "HANDOFF":
        packet = (msg.get("metadata") or {}).get("https://blimp.my/a2a/ext/handoff/v1")
        if not packet:
            errors.append("HANDOFF requires a handoff packet (ext/handoff/v1)")

    return (len(errors) == 0), errors


def wakes_recipient(act: str) -> bool:
    """Does this act wake the recipient (produce a TurnRequest)?"""
    return WAKES_BY_ACT.get(act, True)
