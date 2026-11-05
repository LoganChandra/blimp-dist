#!/usr/bin/env python3
"""blimp-wake gate — runs INSIDE Hermes's gateway before any wake reaches him.

Installed to ~/.hermes/scripts/blimp-wake-gate.py by install.sh. The gateway
pipes the webhook payload in as JSON on stdin; we print the (cleaned) payload
to let the wake through, or exit non-zero to drop it silently.

Server-side enforcement, independent of the client CLI:
  - D-008: agent-to-agent traffic never wakes an agent. A wake must carry
    the Boss's own words (verbatim) — it is a relay of HIS request, not a peer ask.
  - Only known senders (duke, frank/nanook, logan).
  - Budget guard (D-009, Hermes bills OpenRouter): at most MAX_PER_HOUR wakes
    per rolling hour, so a looping agent can't drain the balance.
"""
import json
import os
import sys
import time
from pathlib import Path

SENDERS = {"duke", "frank", "nanook", "logan"}
MAX_PER_HOUR = int(os.environ.get("BLIMP_WAKE_MAX_PER_HOUR", "6"))
LEDGER = Path(os.environ.get("BLIMP_WAKE_LEDGER", str(Path.home() / ".hermes" / "blimp-wake-ledger.json")))
LIMITS = {"logan_said": 2000, "task": 4000, "logan_when": 120, "wake_id": 64}


def drop(reason: str) -> None:
    print(f"blimp-wake gate: dropped ({reason})", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    try:
        p = json.load(sys.stdin)
    except ValueError:
        drop("bad json")
    if not isinstance(p, dict):
        drop("payload not an object")
    sender = str(p.get("from", "")).strip().lower()
    if sender not in SENDERS:
        drop(f"unknown sender {sender!r}")
    clean = {"from": sender}
    for k, cap in LIMITS.items():
        v = str(p.get(k, "")).strip()
        if len(v) > cap:
            drop(f"{k} too long")
        clean[k] = v
    if len(clean["logan_said"]) < 3:
        drop("D-008: no verbatim the Boss request — agents may not wake each other on their own")
    if not clean["task"]:
        drop("empty task")

    now = time.time()
    try:
        hits = [t for t in json.loads(LEDGER.read_text()) if now - t < 3600]
    except (OSError, ValueError):
        hits = []
    if len(hits) >= MAX_PER_HOUR:
        drop(f"budget guard: {MAX_PER_HOUR} wakes in the last hour")
    hits.append(now)
    LEDGER.write_text(json.dumps(hits))

    print(json.dumps(clean))


if __name__ == "__main__":
    main()
