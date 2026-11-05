"""Deterministic kernel sweep (spec 9.4 lease expiry, 10.1 scheduler): runs every minute from a
systemd timer. SQL only, zero tokens. Returns expired leases to READY and alerts Frank."""
from __future__ import annotations

import json
import os
import sys

import psycopg

from ..deck.board import Board
from ..deck.verification import verify
from ..toolgate.gate import Toolgate


def pending_verifications(dsn: str) -> list[str]:
    """IN_REVIEW tasks whose latest review event is verification.started (not yet verified)."""
    with psycopg.connect(dsn) as c:
        return [r[0] for r in c.execute(
            """SELECT t.id FROM mesh.tasks t WHERE t.state='IN_REVIEW' AND (
                 SELECT e.type FROM mesh.events e WHERE e.subject_type='task' AND e.subject_id=t.id
                 AND e.type IN ('verification.started','verification.awaiting_logan','verification.failed','task.done')
                 ORDER BY e.id DESC LIMIT 1) = 'verification.started' ORDER BY t.id""").fetchall()]


def main() -> int:
    dsn = os.environ.get("BLIMPG_DSN") or Board().dsn
    b = Board(dsn)
    out = {"expired_leases": b.expire_leases(), "expired_approvals": Toolgate(dsn).expire_approvals(), "verified": {}}
    for tid in pending_verifications(dsn):
        try:  # one broken task must never stop the sweep for every other task
            out["verified"][tid] = verify(dsn, tid)["verdict"]
        except Exception as e:
            out["verified"][tid] = f"ERROR: {type(e).__name__}: {str(e)[:200]}"
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
