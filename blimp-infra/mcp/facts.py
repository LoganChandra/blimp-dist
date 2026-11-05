#!/usr/bin/env python3
"""
facts.py — the deterministic, bitemporal fact store for the Blimp brain.

The gap this closes: Hindsight dates a memory but never RESOLVES contradictions.
A stale fact ("client on plan A") and its correction ("client on plan B") both
sit in the store and both surface on recall — the reranker just hopes the right
one wins. For a business brain, confidently recalling a stale price or status is
money lost and a hit to "never invent facts" (commitment #1) and C8.

The fix, and the whole design principle: **the LLM may EXTRACT a fact, but CODE
DECIDES which one is current** — a max-timestamp supersession rule, not a model
judgement (the industry's worst-scoring task, ~7% for graph vendors; the
deterministic rule scores 78-95%). This is our own deterministic-core doctrine.

Bitemporal = two clocks per fact:
  - valid_from / valid_to : when the fact was true IN THE WORLD (world time)
  - created_at            : when WE learned it (system/ingestion time)
That gives point-in-time "as-of" queries for free ("what was our quote for this
client in May?") — a WHERE clause once the columns exist.

Trust rule baked into the resolver: an UNVERIFIED write never invalidates a
VERIFIED current fact — it is stored as `contested` instead. This encodes the
standing "verify Hermes's claimed actions — his cheap model fabricates" rule as
mechanism, not vigilance.

Additive + reversible: a brand-new `facts` table in duke_data. Nothing existing
is touched; `DROP TABLE facts` fully reverts. Never touches Hindsight.

Self-test:  python facts.py --selftest      (uses throwaway TEST_ entities)
Schema:     python facts.py --ensure-schema
"""
from __future__ import annotations

import os
import sys
import json
import datetime
from typing import Optional

import psycopg
from psycopg.rows import dict_row

DUKE_DB_URL = os.environ.get(
    "DUKE_DB_URL",
    "postgresql:///duke_data?host=/var/run/postgresql&port=5433",
)

VERIFIED_AUTHORS = {a.strip() for a in
                    os.environ.get("BRAIN_VERIFIED_AUTHORS",
                                   "duke,logan,frank,nanook").split(",")}

DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id             BIGSERIAL PRIMARY KEY,
    entity         TEXT NOT NULL,
    attribute      TEXT NOT NULL,
    value          TEXT NOT NULL,
    valid_from     TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to       TIMESTAMPTZ,
    invalidated_by BIGINT,
    superseded     BOOLEAN NOT NULL DEFAULT FALSE,
    contested      BOOLEAN NOT NULL DEFAULT FALSE,
    author         TEXT NOT NULL DEFAULT 'duke',
    verified       BOOLEAN NOT NULL DEFAULT TRUE,
    confidence     REAL,
    source         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS facts_entity_attr ON facts (entity, attribute);
DROP INDEX IF EXISTS facts_current;
-- UNIQUE: at most ONE current row per entity+attribute. Structurally enforces
-- the deterministic-resolution invariant even if a caller ever skips the lock
-- (defense-in-depth behind the advisory lock in fact_set). Requires the
-- close-old-before-insert-new ordering below so a single txn never has two.
CREATE UNIQUE INDEX IF NOT EXISTS facts_current_uniq
    ON facts (entity, attribute) WHERE valid_to IS NULL AND NOT superseded;
"""


def _conn():
    return psycopg.connect(DUKE_DB_URL, row_factory=dict_row)


def ensure_schema() -> None:
    with _conn() as c:
        c.execute(DDL)
        c.commit()


def _is_verified(author: str, explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return explicit
    return str(author).lower() in VERIFIED_AUTHORS


def _parse_ts(ts) -> Optional[datetime.datetime]:
    if ts is None or isinstance(ts, datetime.datetime):
        return ts
    try:
        return datetime.datetime.fromisoformat(str(ts))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# The write path — CODE decides which fact is current
# ---------------------------------------------------------------------------

def fact_set(entity: str, attribute: str, value: str,
             valid_from=None, author: str = "duke",
             verified: Optional[bool] = None, confidence: Optional[float] = None,
             source: Optional[str] = None) -> dict:
    """Assert that `entity.attribute = value` as of `valid_from` (default now).

    Deterministic resolution against the current open fact for (entity,
    attribute):
      - same value            -> reinforce (no-op supersession), return unchanged
      - different value, and the writer is allowed to supersede
        (verified >= current's verified) -> close the old fact
        (valid_to = valid_from, invalidated_by = new id, superseded = TRUE) and
        insert the new one as current
      - different value, but writer is UNVERIFIED vs a VERIFIED current fact
        -> do NOT overwrite; store the new fact as `contested` (valid_to set so
        it's not returned as current) for a human/peer to adjudicate
    """
    verified = _is_verified(author, verified)
    vf = _parse_ts(valid_from)
    with _conn() as c:
        # Serialize concurrent writers to the SAME entity+attribute. Three agents
        # run three separate MCP processes against one duke_data, so without this
        # two racing fact_set calls both read the same current row and both insert
        # "open" rows — leaving two rows current and letting a later supersede
        # re-expose a stale value, defeating deterministic resolution. This
        # transaction-scoped lock is released on commit at block exit; writers to
        # OTHER keys never block. (P1 from the 2026-07-13 review.)
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                  (entity, attribute))
        vf = vf or _now(c)
        cur = c.execute(
            "SELECT * FROM facts WHERE entity=%s AND attribute=%s "
            "AND valid_to IS NULL AND NOT superseded "
            "ORDER BY valid_from DESC, id DESC LIMIT 1",
            (entity, attribute)).fetchone()

        if cur and _norm(cur["value"]) == _norm(value):
            return {"action": "unchanged", "id": cur["id"],
                    "entity": entity, "attribute": attribute, "value": value}

        if cur and cur["verified"] and not verified:
            # unverified writer cannot overwrite a verified current fact
            new_id = _insert(c, entity, attribute, value, vf, author, verified,
                             confidence, source, contested=True,
                             valid_to=vf)  # closed window => never "current"
            c.commit()
            return {"action": "contested", "id": new_id,
                    "kept_current_id": cur["id"], "kept_value": cur["value"],
                    "entity": entity, "attribute": attribute, "value": value}

        # Backdated correction: the new valid_from is at/before the current
        # fact's valid_from. Don't close the newer current row — thread the
        # correction into HISTORY as [vf, cur.valid_from), superseded by cur, so
        # as-of queries for that past window resolve correctly and the current
        # value is untouched. (Handles the common single-current case; full
        # multi-row interval splitting is a future enhancement.)
        if cur and vf <= cur["valid_from"]:
            hist_id = _insert(c, entity, attribute, value, vf, author, verified,
                              confidence, source, valid_to=cur["valid_from"])
            c.execute("UPDATE facts SET superseded=TRUE, invalidated_by=%s "
                      "WHERE id=%s", (cur["id"], hist_id))
            c.commit()
            return {"action": "backfilled_history", "id": hist_id,
                    "current_id": cur["id"], "entity": entity,
                    "attribute": attribute, "value": value}

        # Forward supersession — close the old row BEFORE inserting the new one,
        # so the UNIQUE current-row index is never even momentarily violated
        # inside the transaction.
        superseded_id = cur["id"] if cur else None
        if superseded_id:
            c.execute("UPDATE facts SET valid_to=%s, superseded=TRUE WHERE id=%s",
                      (vf, superseded_id))
        new_id = _insert(c, entity, attribute, value, vf, author, verified,
                         confidence, source)
        if superseded_id:
            c.execute("UPDATE facts SET invalidated_by=%s WHERE id=%s",
                      (new_id, superseded_id))
        c.commit()
        return {"action": "superseded" if superseded_id else "created",
                "id": new_id, "superseded_id": superseded_id,
                "entity": entity, "attribute": attribute, "value": value}


def _insert(c, entity, attribute, value, valid_from, author, verified,
            confidence, source, contested=False, valid_to=None) -> int:
    row = c.execute(
        "INSERT INTO facts (entity, attribute, value, valid_from, valid_to, "
        "author, verified, confidence, source, contested) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (entity, attribute, value, valid_from, valid_to, author, verified,
         confidence, source, contested)).fetchone()
    return row["id"]


def _now(c) -> datetime.datetime:
    return c.execute("SELECT now() AS n").fetchone()["n"]


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


# ---------------------------------------------------------------------------
# The read path — current value, or point-in-time
# ---------------------------------------------------------------------------

def fact_get(entity: str, attribute: str, as_of=None) -> Optional[dict]:
    """Current value for entity.attribute, or the value in effect AT `as_of`
    (point-in-time). Returns the row dict, or None."""
    with _conn() as c:
        if as_of is None:
            return c.execute(
                "SELECT * FROM facts WHERE entity=%s AND attribute=%s "
                "AND valid_to IS NULL AND NOT superseded "
                "ORDER BY valid_from DESC, id DESC LIMIT 1",
                (entity, attribute)).fetchone()
        ao = _parse_ts(as_of)
        return c.execute(
            "SELECT * FROM facts WHERE entity=%s AND attribute=%s "
            "AND NOT contested AND valid_from <= %s "
            "AND (valid_to IS NULL OR valid_to > %s) "
            "ORDER BY valid_from DESC, id DESC LIMIT 1",
            (entity, attribute, ao, ao)).fetchone()


def fact_history(entity: str, attribute: Optional[str] = None) -> list[dict]:
    with _conn() as c:
        if attribute:
            return c.execute(
                "SELECT * FROM facts WHERE entity=%s AND attribute=%s "
                "ORDER BY valid_from, id", (entity, attribute)).fetchall()
        return c.execute(
            "SELECT * FROM facts WHERE entity=%s ORDER BY attribute, valid_from, id",
            (entity,)).fetchall()


def entity_state(entity: str) -> dict:
    """All current (attribute -> value) facts for an entity — a live profile."""
    with _conn() as c:
        rows = c.execute(
            "SELECT DISTINCT ON (attribute) attribute, value, valid_from, author, "
            "verified FROM facts WHERE entity=%s AND valid_to IS NULL "
            "AND NOT superseded ORDER BY attribute, valid_from DESC, id DESC",
            (entity,)).fetchall()
    return {r["attribute"]: r["value"] for r in rows}


def contested_facts(limit: int = 50) -> list[dict]:
    """Open contested claims awaiting adjudication (the 'someone disagreed' list)."""
    with _conn() as c:
        return c.execute(
            "SELECT * FROM facts WHERE contested ORDER BY created_at DESC LIMIT %s",
            (limit,)).fetchall()


def _cleanup_test_entities():
    with _conn() as c:
        c.execute("DELETE FROM facts WHERE entity LIKE 'TEST\\_%'")
        c.commit()


def _count_current(entity: str, attribute: str) -> int:
    with _conn() as c:
        return c.execute(
            "SELECT count(*) AS n FROM facts WHERE entity=%s AND attribute=%s "
            "AND valid_to IS NULL AND NOT superseded",
            (entity, attribute)).fetchone()["n"]


# ---------------------------------------------------------------------------
# Self-test — deterministic, uses throwaway TEST_ entities, cleans up after
# ---------------------------------------------------------------------------

def _selftest() -> int:
    ensure_schema()
    _cleanup_test_entities()
    E = "TEST_client_acme"
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    t1 = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc)
    t3 = datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)

    r1 = fact_set(E, "plan", "A", valid_from=t1, author="duke")
    check("first write is 'created'", r1["action"] == "created")

    r2 = fact_set(E, "plan", "B", valid_from=t2, author="duke")
    check("contradiction supersedes", r2["action"] == "superseded"
          and r2["superseded_id"] == r1["id"])

    cur = fact_get(E, "plan")
    check("current value is newest (B)", cur and cur["value"] == "B")

    asof_may = fact_get(E, "plan", as_of=datetime.datetime(2026, 5, 15,
                        tzinfo=datetime.timezone.utc))
    check("as-of May returns old value (A)", asof_may and asof_may["value"] == "A")

    asof_jun = fact_get(E, "plan", as_of=datetime.datetime(2026, 6, 15,
                        tzinfo=datetime.timezone.utc))
    check("as-of June returns new value (B)", asof_jun and asof_jun["value"] == "B")

    r_same = fact_set(E, "plan", "B", author="duke")
    check("same value is a no-op", r_same["action"] == "unchanged")

    # unverified writer must NOT overwrite a verified current fact
    r_unv = fact_set(E, "plan", "C", author="hermes", verified=False)
    check("unverified write is contested, not applied", r_unv["action"] == "contested")
    check("current value still B after unverified attempt",
          fact_get(E, "plan")["value"] == "B")
    check("contested fact is listed",
          any(f["value"] == "C" for f in contested_facts()))

    hist = fact_history(E, "plan")
    check("history has full chain (>=3 rows)", len(hist) >= 3)

    r3 = fact_set(E, "status", "active", valid_from=t3, author="logan")
    st = entity_state(E)
    check("entity_state shows current plan=B and status=active",
          st.get("plan") == "B" and st.get("status") == "active")

    # backdated correction: threads into history, leaves current untouched
    fact_set(E, "tier", "gold", valid_from=t3, author="logan")
    rb = fact_set(E, "tier", "bronze",
                  valid_from=datetime.datetime(2026, 4, 1, tzinfo=datetime.timezone.utc),
                  author="logan")
    check("backdated write threads into history",
          rb["action"] == "backfilled_history")
    check("current tier unchanged after backdate",
          fact_get(E, "tier")["value"] == "gold")
    check("as-of before current returns the backdated value",
          fact_get(E, "tier", as_of=datetime.datetime(2026, 4, 15,
                   tzinfo=datetime.timezone.utc))["value"] == "bronze")
    check("exactly one current row for tier (unique-current invariant)",
          _count_current(E, "tier") == 1)
    check("exactly one current row for plan after all writes",
          _count_current(E, "plan") == 1)

    _cleanup_test_entities()
    check("cleanup removed test rows", not fact_history(E, "plan"))

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


def main():
    if "--ensure-schema" in sys.argv:
        ensure_schema(); print("facts schema ensured."); return
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)


if __name__ == "__main__":
    main()
