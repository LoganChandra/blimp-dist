#!/usr/bin/env python3
"""
procedural.py — procedural ("how-to") memory for the Blimp brain.

The gap this closes: our brain stores semantic + episodic memory (facts,
decisions, chat) but has NO procedural memory — the ability to turn "Duke did X
this way and it worked" into a retrievable how-to that future runs reuse.
Today that only happens when a human hand-writes a skill. This makes it a first-
class, auto-accumulating layer (Memp / Agent-Workflow-Memory: +8-35pp task
success), which operationalises C6 (self-improvement is structural).

Design choices that fit us specifically:
- The one signal Memp needs — "did this run actually work?" — we already produce
  via the V1-V5 verification ladder. Outcomes feed success/fail counts.
- We do NOT auto-graduate. A distilled procedure starts `approved=FALSE` (a
  candidate); a human/peer approves it before it's served by default — matching
  the standing "don't let the brain silently rewrite itself" discipline.
- Deterministic, dependency-free retrieval (token overlap over name/trigger/
  tags/steps, tie-broken by success rate). Semantic retrieval via Hindsight is a
  later enhancement; v1 is bulletproof and testable with no embedding infra.

Additive + reversible: a new `procedures` table in duke_data. `DROP TABLE
procedures` fully reverts. Never touches Hindsight or existing tables.

Self-test:  python procedural.py --selftest
Schema:     python procedural.py --ensure-schema
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional

import psycopg
from psycopg.rows import dict_row

DUKE_DB_URL = os.environ.get(
    "DUKE_DB_URL",
    "postgresql:///duke_data?host=/var/run/postgresql&port=5433",
)

DDL = """
CREATE TABLE IF NOT EXISTS procedures (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    trigger       TEXT NOT NULL DEFAULT '',
    steps         TEXT NOT NULL,
    tags          TEXT NOT NULL DEFAULT '',
    success_count INTEGER NOT NULL DEFAULT 0,
    fail_count    INTEGER NOT NULL DEFAULT 0,
    last_outcome  TEXT,
    last_used_at  TIMESTAMPTZ,
    source        TEXT,
    author        TEXT NOT NULL DEFAULT 'duke',
    approved      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS procedures_approved ON procedures (approved);
CREATE UNIQUE INDEX IF NOT EXISTS procedures_name ON procedures (lower(name));
"""


def _conn():
    return psycopg.connect(DUKE_DB_URL, row_factory=dict_row)


def ensure_schema() -> None:
    with _conn() as c:
        c.execute(DDL)
        c.commit()


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(t) > 2}


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def procedure_add(name: str, steps: str, trigger: str = "", tags: str = "",
                  source: Optional[str] = None, author: str = "duke",
                  approved: bool = False) -> dict:
    """Draft or update a procedure. Same name => update in place (idempotent
    distillation). New procedures start unapproved (a candidate for review)."""
    with _conn() as c:
        existing = c.execute("SELECT id FROM procedures WHERE lower(name)=lower(%s)",
                             (name,)).fetchone()
        if existing:
            # Re-gate: any content change resets approval to FALSE, so an
            # already-approved procedure can never silently serve re-distilled /
            # unreviewed steps (the module's stated safety property — a human
            # re-approves). Costs a re-approval on edits; that's the point.
            c.execute(
                "UPDATE procedures SET steps=%s, trigger=%s, tags=%s, source=%s, "
                "author=%s, approved=FALSE, updated_at=now() WHERE id=%s",
                (steps, trigger, tags, source, author, existing["id"]))
            c.commit()
            return {"action": "updated", "id": existing["id"], "name": name,
                    "reapproval_required": True}
        row = c.execute(
            "INSERT INTO procedures (name, trigger, steps, tags, source, author, "
            "approved) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (name, trigger, steps, tags, source, author, approved)).fetchone()
        c.commit()
        return {"action": "created", "id": row["id"], "name": name,
                "approved": approved}


def procedure_record_outcome(name_or_id, success: bool,
                             note: Optional[str] = None) -> dict:
    """Record a run outcome — feeds the success/fail signal (from V1-V5)."""
    col = "success_count" if success else "fail_count"
    with _conn() as c:
        row = _find(c, name_or_id)
        if not row:
            return {"error": f"no procedure {name_or_id!r}"}
        c.execute(
            f"UPDATE procedures SET {col}={col}+1, last_outcome=%s, "
            "last_used_at=now(), updated_at=now() WHERE id=%s",
            ("success" if success else "fail" if not note else note, row["id"]))
        c.commit()
        return {"id": row["id"], "name": row["name"], "recorded":
                "success" if success else "fail"}


def procedure_approve(name_or_id, approved: bool = True) -> dict:
    with _conn() as c:
        row = _find(c, name_or_id)
        if not row:
            return {"error": f"no procedure {name_or_id!r}"}
        c.execute("UPDATE procedures SET approved=%s, updated_at=now() WHERE id=%s",
                  (approved, row["id"]))
        c.commit()
        return {"id": row["id"], "name": row["name"], "approved": approved}


def _find(c, name_or_id):
    if isinstance(name_or_id, int) or str(name_or_id).isdigit():
        return c.execute("SELECT * FROM procedures WHERE id=%s",
                         (int(name_or_id),)).fetchone()
    return c.execute("SELECT * FROM procedures WHERE lower(name)=lower(%s)",
                     (name_or_id,)).fetchone()


# ---------------------------------------------------------------------------
# Read — retrieve the how-to for a task at task-start
# ---------------------------------------------------------------------------

def procedure_recall(query: str, limit: int = 3,
                     only_approved: bool = True) -> list[dict]:
    """Find the procedures most relevant to a task. Deterministic token-overlap
    score over name/trigger/tags/steps, boosted by a proven success rate, so a
    battle-tested how-to outranks an untried one."""
    q = _tokens(query)
    if not q:
        return []
    with _conn() as c:
        where = "WHERE approved" if only_approved else ""
        rows = c.execute(f"SELECT * FROM procedures {where}").fetchall()
    scored = []
    for r in rows:
        hay = _tokens(f"{r['name']} {r['trigger']} {r['tags']} {r['steps']}")
        overlap = len(q & hay)
        if not overlap:
            continue
        total = r["success_count"] + r["fail_count"]
        success_rate = (r["success_count"] / total) if total else 0.5
        # relevance dominates; success rate breaks ties / nudges proven ones up
        score = overlap + 0.5 * success_rate + 0.05 * r["success_count"]
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        out.append({
            "id": r["id"], "name": r["name"], "trigger": r["trigger"],
            "steps": r["steps"], "tags": r["tags"],
            "success_count": r["success_count"], "fail_count": r["fail_count"],
            "score": round(score, 2),
        })
    return out


def procedure_list(approved: Optional[bool] = None, limit: int = 100) -> list[dict]:
    with _conn() as c:
        if approved is None:
            return c.execute("SELECT * FROM procedures ORDER BY updated_at DESC "
                             "LIMIT %s", (limit,)).fetchall()
        return c.execute("SELECT * FROM procedures WHERE approved=%s "
                         "ORDER BY updated_at DESC LIMIT %s",
                         (approved, limit)).fetchall()


def _cleanup_test():
    with _conn() as c:
        c.execute("DELETE FROM procedures WHERE name LIKE 'TEST\\_%'")
        c.commit()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _selftest() -> int:
    ensure_schema()
    _cleanup_test()
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    r = procedure_add(
        "TEST_deploy_blimp_site",
        steps="1. git push to master 2. wait for Vercel 3. verify with one Playwright load",
        trigger="deploying the blimp.my portfolio site",
        tags="deploy,vercel,blimp", author="duke")
    check("created as candidate", r["action"] == "created")

    # unapproved -> not served by default
    check("unapproved not recalled by default",
          procedure_recall("how do I deploy the blimp site") == [])

    procedure_approve("TEST_deploy_blimp_site")
    hits = procedure_recall("how do I deploy the blimp site to vercel")
    check("approved procedure is recalled", len(hits) == 1
          and hits[0]["name"] == "TEST_deploy_blimp_site")

    procedure_record_outcome("TEST_deploy_blimp_site", success=True)
    procedure_record_outcome("TEST_deploy_blimp_site", success=True)
    procedure_record_outcome("TEST_deploy_blimp_site", success=False, note="vercel bot-mitigation lockout")
    row = _one("TEST_deploy_blimp_site")
    check("outcomes recorded (2 success, 1 fail)",
          row["success_count"] == 2 and row["fail_count"] == 1)
    check("last_outcome note stored", row["last_outcome"] == "vercel bot-mitigation lockout")

    # a proven procedure outranks an unproven one on the same query
    procedure_add("TEST_deploy_other", steps="deploy something", trigger="deploy vercel",
                  tags="deploy,vercel", approved=True)
    ranked = procedure_recall("deploy to vercel", limit=2)
    check("proven procedure ranks first",
          ranked and ranked[0]["name"] == "TEST_deploy_blimp_site")

    upd = procedure_add("TEST_deploy_blimp_site", steps="updated steps",
                        trigger="deploying blimp", tags="deploy")
    check("same name updates in place", upd["action"] == "updated")
    check("content update re-gates approval (no silent serve of new steps)",
          all(h["name"] != "TEST_deploy_blimp_site"
              for h in procedure_recall("deploy blimp site to vercel",
                                        only_approved=True)))

    _cleanup_test()
    check("cleanup removed test rows", not procedure_list(limit=500) or
          all(not p["name"].startswith("TEST_") for p in procedure_list(limit=500)))

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


def _one(name):
    with _conn() as c:
        return _find(c, name)


def main():
    if "--ensure-schema" in sys.argv:
        ensure_schema(); print("procedures schema ensured."); return
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)


if __name__ == "__main__":
    main()
