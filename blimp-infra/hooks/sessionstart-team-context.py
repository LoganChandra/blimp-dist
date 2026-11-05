#!/usr/bin/env python3
"""sessionstart-team-context.py: each agent starts a session already knowing what the others just did.

A Claude Code SessionStart hook. Its stdout is added to the session's context. It prints the newest
Discord turns from the OTHER agents' channels (auto-captured by stop-brain-capture.py), newest first, so
Duke opens a session knowing what the Boss said to Frank today without having to think of asking. This is the
"inject recent observations at session start" pattern (claude-mem's SessionStart hook; Letta's shared
memory blocks).

Fails open: any error prints nothing and exits 0. Hard cap of about 3 KB so it never floods the context.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "postgres"))

CHANNELS = {"duke": "Duke", "nanook": "Frank", "hermes": "Hermes"}
PER_CHANNEL = 6
CAP = 3000


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    me = os.getenv("BLIMP_AGENT") or ("nanook" if "nanook" in (payload.get("cwd") or os.getcwd()) else "duke")
    try:
        import hindsight_client as hc

        lines = []
        for ch, name in CHANNELS.items():
            if ch == me:
                continue
            rows, kept = hc.recent(channel=ch, limit=PER_CHANNEL * 3), []
            for r in rows:  # one turn is often stored as several near-identical facts
                if not any(hc._text_ratio(r.get("text") or "", k.get("text") or "") >= 0.6 for k in kept):
                    kept.append(r)
            rows = kept[:PER_CHANNEL]
            if not rows:
                continue
            lines.append(f"{name} (#{ch}), newest first:")
            for r in rows:
                when = hc._when(r)[:16].replace("T", " ")
                text = " ".join((r.get("text") or "").split())[:220]
                lines.append(f"  - {when} UTC: {text}")
        if lines:
            out = "Team brain: what the other agents did most recently (auto-captured).\n" + "\n".join(lines)
            print(out[:CAP])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
