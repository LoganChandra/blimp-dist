#!/usr/bin/env python3
"""hermes-wake — wake Hermes and hand him a task, on the Boss's behalf.

The Hermes equivalent of `duke-up` + typing into Duke's tmux pane. Hermes is
not a tmux session; he is an always-running gateway (hermes-gateway.service,
127.0.0.1:8644). So a wake is a signed POST to his `blimp-wake` webhook route,
which runs one Hermes turn on the handoff and posts his reply in #hermes.

  hermes-wake --from frank \\
      --logan-said "can you ask him to figure out X" --logan-when "2026-01-01 09:00 #frank" \\
      --task "Figure out X. Context: ..."

Rules baked in (and enforced again server-side by gate.py):
  - D-008: only the Boss-initiated work wakes an agent. --logan-said (his words,
    verbatim) is required; a peer-only ask goes through A2A/the board instead.
  - Zero idle cost: nothing runs until a wake is sent; each wake = one turn.
  - Every wake is logged to the shared brain (channel `hermes`) so his next
    full session sees the handoff even if the woken turn can't finish it.

Exit codes: 0 accepted, 2 refused by this CLI, 3 gateway down, 4 rejected by gateway
(4 with "Unknown route" = the blimp-wake route is not activated yet; see README).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

GATEWAY = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8644")
ROUTE = os.environ.get("HERMES_WAKE_ROUTE", "blimp-wake")
SECRET_FILE = Path(os.path.expanduser(os.environ.get("HERMES_WAKE_SECRET_FILE", "~/blimp/secrets/hermes-wake.secret")))
SENDERS = ("duke", "frank", "nanook", "logan")


def gateway_up() -> bool:
    try:
        with urllib.request.urlopen(f"{GATEWAY}/health", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def ensure_gateway(wait_s: int = 45) -> bool:
    """Like duke-up: if Hermes's gateway is down, start his service and wait."""
    if gateway_up():
        return True
    print("[hermes-wake] gateway down — starting hermes-gateway.service", file=sys.stderr)
    try:
        subprocess.run(["systemctl", "--user", "start", "hermes-gateway.service"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[hermes-wake] could not start gateway: {e}", file=sys.stderr)
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(2)
        if gateway_up():
            return True
    return False


def sign(secret: str, body: bytes, ts: int) -> str:
    return hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()


def brain_log(sender: str, msg: str) -> None:
    try:
        subprocess.run(["duke-mem", "log", "--user", "logan", "--channel", "hermes",
                        "--speaker", sender, "--message", msg], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[hermes-wake] brain log skipped: {e}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="sender", required=True, choices=SENDERS)
    ap.add_argument("--logan-said", required=True, help="the Boss's request, VERBATIM (C5)")
    ap.add_argument("--logan-when", default="", help="when/where he said it, e.g. '2026-01-01 09:00 #frank'")
    ap.add_argument("--task", required=True, help="the handoff: what you need from Hermes + context")
    ap.add_argument("--dry-run", action="store_true", help="print the signed request, send nothing")
    ap.add_argument("--no-brain-log", action="store_true")
    a = ap.parse_args(argv)

    if len(a.logan_said.strip()) < 3:
        print("[hermes-wake] refused: D-008 — a wake must relay the Boss's own words (--logan-said). "
              "For a peer-only ask, use A2A / the board; it will not wake Hermes.", file=sys.stderr)
        return 2
    if not a.task.strip():
        print("[hermes-wake] refused: empty --task", file=sys.stderr)
        return 2
    try:
        secret = SECRET_FILE.read_text().strip()
    except OSError:
        print(f"[hermes-wake] no secret at {SECRET_FILE} — the blimp-wake route is not activated yet (see README)", file=sys.stderr)
        return 2

    wake_id = f"W-{uuid.uuid4().hex[:8]}"
    payload = {"event_type": "blimp.wake", "wake_id": wake_id, "from": a.sender,
               "logan_said": a.logan_said.strip(), "logan_when": a.logan_when.strip(), "task": a.task.strip()}
    body = json.dumps(payload).encode()
    ts = int(time.time())
    headers = {"Content-Type": "application/json", "X-Webhook-Timestamp": str(ts),
               "X-Webhook-Signature-V2": sign(secret, body, ts), "X-Request-ID": wake_id}
    if a.dry_run:
        print(json.dumps({"url": f"{GATEWAY}/webhooks/{ROUTE}", "payload": payload}, indent=2))
        return 0

    if not ensure_gateway():
        print("[hermes-wake] Hermes gateway is down and did not come back — wake NOT delivered", file=sys.stderr)
        return 3
    req = urllib.request.Request(f"{GATEWAY}/webhooks/{ROUTE}", data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read() or b"{}")
            code = r.status
    except urllib.error.HTTPError as e:
        resp, code = json.loads(e.read() or b"{}"), e.code
    except OSError as e:
        print(f"[hermes-wake] gateway unreachable: {e}", file=sys.stderr)
        return 3

    if code >= 300 or resp.get("status") == "ignored":
        print(f"[hermes-wake] REJECTED by gateway (HTTP {code}): {json.dumps(resp)}", file=sys.stderr)
        return 4
    if not a.no_brain_log:
        brain_log(a.sender, f"[wake {wake_id}] {a.sender} woke Hermes on the Boss's request "
                            f"(\"{a.logan_said.strip()}\" {a.logan_when.strip()}). Task: {a.task.strip()}")
    print(f"[hermes-wake] accepted {wake_id} (HTTP {code}) — Hermes is on it; his reply lands in #hermes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
