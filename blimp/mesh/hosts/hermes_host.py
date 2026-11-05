"""Hermes adapter (spec §10.4): Deck turns delivered to the existing Hermes Agent gateway.

Hermes runs on her own runtime (Hermes Agent, OpenRouter; D-009/D-010). The kernel side is the
same Host as Duke and Frank (claim, lease-bound gate turn, context pack, manifest, Rule #0,
poison handling); only the runner differs:
  - it POSTs the turn to the gateway webhook (HMAC-SHA256 signed, as v1 did), with the context
    pack and the instruction to use ONLY the blimp-toolgate tools and to finish with sys_turn_done;
  - the gateway's `crew` profile points its MCP config at the Toolgate server with
    BLIMP_AGENT=hermes and no turn id: the server binds each call to the turn the kernel is
    running for Hermes, so the model can never pick a turn;
  - the turn ends when Hermes calls sys_turn_done, or at the wall-clock limit (failure: no
    completion signal).
Profile setup (crew profile, MCP -> Toolgate, cron off for crew work, Discord platform decision
G-7) is a change to Hermes's own config and is listed in the go-live checklist; not done here.
Zero LLM in this file.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from blimp.mesh.hosts.claude_host import Host  # noqa: E402

WEBHOOK_URL = os.environ.get("HERMES_WEBHOOK_URL", "http://127.0.0.1:8644/webhooks/a2a-task")
POLL_S = 5


def webhook_runner(dsn: str, secret: str, url: str = WEBHOOK_URL, opener: Callable = urllib.request.urlopen,
                   sleep: Callable[[float], None] = time.sleep) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if not secret:
        raise ValueError("HERMES_WEBHOOK_SECRET is required (the gateway rejects unsigned turns)")

    def run(req: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({
            "task_id": req["turn_id"],
            "instruction": (req["system_prompt"] + "\n\n" + req["user_prompt"] +
                            "\nUse ONLY the blimp-toolgate tools for this turn. Pass turn_key=\"" + req["turn_key"] + "\" on EVERY "
                            "toolgate call (it binds your calls to this turn; calls without it are refused). When you are "
                            "finished, call sys_turn_done."),
        }).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = urllib.request.Request(url, data=body, method="POST",
                                   headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={sig}"})
        try:
            with opener(r, timeout=15) as resp:
                if resp.status >= 300:
                    return {"ok": False, "error": f"gateway answered HTTP {resp.status}"}
        except Exception as e:
            return {"ok": False, "error": f"gateway unreachable: {e}"}
        deadline = time.monotonic() + req["wall_clock_s"]
        while time.monotonic() < deadline:
            with psycopg.connect(dsn) as c:
                done = c.execute("SELECT payload FROM mesh.events WHERE type='turn.done_signal' AND subject_id=%s LIMIT 1",
                                 (req["turn_id"],)).fetchone()
            if done:
                return {"ok": True, "result": done[0].get("summary"), "usage": {}, "model": "hermes-gateway"}
            sleep(POLL_S)
        return {"ok": False, "error": "no completion signal from Hermes before the wall-clock limit"}
    return run


def main() -> int:
    dsn = os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")
    Host("hermes", dsn, runner=webhook_runner(dsn, os.environ.get("HERMES_WEBHOOK_SECRET", ""))).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
