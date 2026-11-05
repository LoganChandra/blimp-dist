"""Deck kernel daemon (spec §10.1): the one always-on process that keeps the crew moving.

Every few seconds: route new events into turn requests (dispatcher). Every minute: recover
crashed turns, expire leases and approvals, run pending verifications, and the zero-token
health probes (replacing the v1 heartbeat timer). Everything here is SQL, HTTP, TCP or
filesystem work: zero tokens while idle, which ops/idle_burn.py measures.

Usage: python -m blimp.mesh.deck.daemon [--once]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from blimp.mesh.deck.board import Board  # noqa: E402
from blimp.mesh.deck.dispatch import Dispatcher  # noqa: E402
from blimp.mesh.ops import health  # noqa: E402
from blimp.mesh.ops.sweep import pending_verifications  # noqa: E402
from blimp.mesh.toolgate.gate import Toolgate  # noqa: E402

ROUTE_EVERY_S = 3
MINUTE_EVERY_S = 60


def minute(dsn: str) -> dict:
    from blimp.mesh.deck.verification import verify

    out = {"recovered": Dispatcher(dsn).recover(), "expired_leases": Board(dsn).expire_leases(),
           "expired_approvals": Toolgate(dsn).expire_approvals(), "verified": {}}
    for tid in pending_verifications(dsn):
        try:
            out["verified"][tid] = verify(dsn, tid)["verdict"]
        except Exception as e:
            out["verified"][tid] = f"ERROR: {type(e).__name__}: {e}"[:200]
    try:
        out["health"] = health.record(dsn, health.probes(dsn))
    except Exception as e:  # health must never take the router down
        out["health"] = {"error": str(e)[:200]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    dsn = os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")
    d = Dispatcher(dsn)
    last_minute = 0.0
    while True:
        routed = d.route()
        if time.monotonic() - last_minute >= MINUTE_EVERY_S or a.once:
            m = minute(dsn)
            last_minute = time.monotonic()
            if a.once:
                print(json.dumps({"routed": routed, **m}, default=str))
                return 0
        time.sleep(ROUTE_EVERY_S)


if __name__ == "__main__":
    sys.exit(main())
