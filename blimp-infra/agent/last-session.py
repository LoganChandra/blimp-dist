#!/usr/bin/env python3
"""last-session.py [cwd]: print the id of the agent's last real Discord session in cwd, or nothing.

A restart should pick up the thread the agent was in, not a blank page. `claude --continue` alone
isn't safe for that: it takes the newest transcript in the folder, and headless runs (evals,
`claude -p`) write there too, some of them full of fake Discord messages. So a session counts only
if it was interactive (every `entrypoint` is "cli") and a real Discord message reached it
(a queue-operation enqueue carrying a plugin:discord channel tag). Stdlib only, reads each
candidate once, newest first, and stops at the first match.
"""
import json
import os
import re
import sys
from pathlib import Path

DISCORD = b"plugin:discord"


def project_dir(cwd: str) -> Path:
    # Claude Code names a project folder after the real path with every non-alphanumeric made "-".
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))


def is_channel_session(path: Path) -> bool:
    interactive = real_msg = False
    with path.open("rb") as f:
        for line in f:
            if b'"entrypoint"' in line:
                try:
                    ep = json.loads(line).get("entrypoint")
                except Exception:
                    ep = None
                if ep and ep != "cli":
                    return False  # headless or SDK run: never resume it
                interactive = interactive or ep == "cli"
            if not real_msg and DISCORD in line and b'"queue-operation"' in line:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                real_msg = d.get("operation") == "enqueue" and "plugin:discord" in (d.get("content") or "")
    return interactive and real_msg


def last_session(cwd: str) -> str:
    d = project_dir(cwd)
    if not d.is_dir():
        return ""
    for p in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            if is_channel_session(p):
                return p.stem
        except OSError:
            continue
    return ""


if __name__ == "__main__":
    print(last_session(sys.argv[1] if len(sys.argv) > 1 else os.getcwd()))
