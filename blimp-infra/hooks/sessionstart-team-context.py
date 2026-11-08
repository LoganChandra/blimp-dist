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


def agent_for(cwd: str) -> str:
    """Same rule as stop-brain-capture.py."""
    if os.getenv("BLIMP_AGENT"):
        return os.environ["BLIMP_AGENT"]
    c = cwd.lower()
    if "nanook" in c:
        return "nanook"
    if "blimp" in c or "duke" in c:
        return "duke"
    return ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    me = agent_for(payload.get("cwd") or os.getcwd())
    if not me:  # not a Blimp agent (another project on this PC): Blimp's brain stays out of that session
        return 0
    try:
        import hindsight_client as hc

        lines = []
        for ch, name in CHANNELS.items():
            if ch == me:
                continue
            rows, kept = hc.recent(channel=ch, limit=PER_CHANNEL * 3), []
            rows = [r for r in rows if not private(r)]
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
    alert = capture_alert()
    if alert:
        print(alert)
    return 0


def private(row: dict) -> bool:
    """DMs stay out of other agents' session context (personal content goes to DMs; Duke serves visitors)."""
    tags = row.get("tags") or []
    meta = row.get("metadata") or {}
    return any(str(t).startswith("discord:dm-") for t in tags) or str(meta.get("discord_channel", "")).startswith("dm-")


def capture_alert() -> str:
    """One line when the Stop-hook capture's latest result is a failure, so the agent tells the Boss."""
    log = Path(os.getenv("BLIMP_CAPTURE_STATE") or Path.home() / ".cache" / "blimp-brain-capture") / "capture.log"
    try:
        last = log.read_text().strip().splitlines()[-1]
    except Exception:
        return ""
    if " ERROR " not in last:
        return ""
    return ("Brain capture is FAILING (Discord turns are not reaching the team brain). Last error: "
            f"{last[:300]}. Tell the Boss in one line and suggest checking the Hindsight container.")


if __name__ == "__main__":
    sys.exit(main())
