#!/usr/bin/env python3
"""
blackboard.py — a shared coordination board for the 3-agent team.

Beyond our A2A: A2A is pull-only, addressed, point-to-point (a2a_send to a named
agent). A blackboard is broadcast + opportunistic self-selection — post "who can
verify this quote?" and whoever is best-placed claims it, with all open work
visible in one place. This dissolves the "no-tasks-to-Hermes" friction: nobody
is assigned; agents self-select from the board (bMAS, +13-57% end-to-end over
master-slave). It complements A2A, it does not replace it.

Additive + reversible: a new `blackboard` table in duke_data. Never touches
Hindsight or existing tables. Inert until an agent posts to it.

Self-test:  python blackboard.py --selftest
Schema:     python blackboard.py --ensure-schema
"""
from __future__ import annotations

import os
import sys
from typing import Optional

import psycopg
from psycopg.rows import dict_row

DUKE_DB_URL = os.environ.get(
    "DUKE_DB_URL",
    "postgresql:///duke_data?host=/var/run/postgresql&port=5433",
)

DDL = """
CREATE TABLE IF NOT EXISTS blackboard (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'task',   -- task | question | fyi
    status      TEXT NOT NULL DEFAULT 'open',   -- open | claimed | resolved
    posted_by   TEXT NOT NULL DEFAULT 'duke',
    claimed_by  TEXT,
    tags        TEXT NOT NULL DEFAULT '',
    resolution  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS blackboard_status ON blackboard (status, created_at DESC);
"""


def _conn():
    return psycopg.connect(DUKE_DB_URL, row_factory=dict_row)


def ensure_schema() -> None:
    with _conn() as c:
        c.execute(DDL)
        c.commit()


def board_post(title: str, body: str = "", kind: str = "task",
               tags: str = "", posted_by: str = "duke") -> dict:
    with _conn() as c:
        row = c.execute(
            "INSERT INTO blackboard (title, body, kind, tags, posted_by) "
            "VALUES (%s,%s,%s,%s,%s) RETURNING id",
            (title, body, kind, tags, posted_by)).fetchone()
        c.commit()
        return {"action": "posted", "id": row["id"], "title": title,
                "kind": kind, "status": "open"}


def board_list(status: Optional[str] = "open", limit: int = 50) -> list[dict]:
    with _conn() as c:
        if status:
            return c.execute(
                "SELECT * FROM blackboard WHERE status=%s "
                "ORDER BY created_at DESC LIMIT %s", (status, limit)).fetchall()
        return c.execute("SELECT * FROM blackboard ORDER BY created_at DESC "
                         "LIMIT %s", (limit,)).fetchall()


def board_claim(post_id: int, claimed_by: str = "duke") -> dict:
    """Atomically claim an OPEN item — the WHERE status='open' guard makes the
    claim a race-safe compare-and-swap, so two agents can't both win it."""
    with _conn() as c:
        row = c.execute(
            "UPDATE blackboard SET status='claimed', claimed_by=%s, updated_at=now() "
            "WHERE id=%s AND status='open' RETURNING id, title, claimed_by",
            (claimed_by, post_id)).fetchone()
        c.commit()
        if not row:
            cur = c.execute("SELECT status, claimed_by FROM blackboard WHERE id=%s",
                            (post_id,)).fetchone()
            return {"action": "claim_failed", "id": post_id,
                    "reason": f"not open (status={cur['status'] if cur else 'missing'}, "
                              f"claimed_by={cur['claimed_by'] if cur else None})"}
        return {"action": "claimed", "id": row["id"], "claimed_by": row["claimed_by"]}


def board_respond(post_id: int, resolution: str, resolve: bool = True,
                  by: str = "duke") -> dict:
    status = "resolved" if resolve else "claimed"
    with _conn() as c:
        row = c.execute(
            "UPDATE blackboard SET status=%s, resolution=%s, "
            "claimed_by=COALESCE(claimed_by,%s), updated_at=now() "
            "WHERE id=%s RETURNING id, status",
            (status, resolution, by, post_id)).fetchone()
        c.commit()
        if not row:
            return {"action": "respond_failed", "id": post_id, "reason": "missing"}
        return {"action": "resolved" if resolve else "responded",
                "id": row["id"], "status": row["status"]}


def _cleanup_test():
    with _conn() as c:
        c.execute("DELETE FROM blackboard WHERE title LIKE 'TEST\\_%'")
        c.commit()


def _selftest() -> int:
    ensure_schema()
    _cleanup_test()
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    p = board_post("TEST_verify_quote", "Can someone verify the RM1500 quote for acme?",
                   kind="question", tags="quote,verify", posted_by="duke")
    check("post creates an open item", p["action"] == "posted")
    pid = p["id"]

    check("open item shows on the board",
          any(x["id"] == pid for x in board_list("open")))

    c1 = board_claim(pid, claimed_by="frank")
    check("first claim succeeds", c1["action"] == "claimed" and c1["claimed_by"] == "frank")

    c2 = board_claim(pid, claimed_by="hermes")
    check("second claim fails (race-safe CAS)", c2["action"] == "claim_failed")

    check("claimed item not in open list",
          not any(x["id"] == pid for x in board_list("open")))

    r = board_respond(pid, "Verified — matches the pricing framework.", resolve=True, by="frank")
    check("respond resolves the item", r["action"] == "resolved")
    check("resolved item carries resolution",
          board_list(None)[0]["resolution"].startswith("Verified"))

    _cleanup_test()
    check("cleanup removed test rows",
          not any(x["title"].startswith("TEST_") for x in board_list(None, limit=500)))

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


def main():
    if "--ensure-schema" in sys.argv:
        ensure_schema(); print("blackboard schema ensured."); return
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)


if __name__ == "__main__":
    main()
