"""Import v1 work into the board (spec §15 "Migration mapping", §23 phase 1).

  public.tasks         -> mesh.tasks   (contract backfilled as {"legacy": true, ...})
  public.a2a_messages  -> mesh.messages (one legacy thread per v1 A2A task id)

Rules:
- Idempotent: mesh.import_map records every imported row; re-running imports only new rows.
- Nothing is invented. Fields v1 never had stay empty; the legacy contract says so.
- v1 'pending' becomes PROPOSED (it has no v2 contract, so it cannot be READY until someone
  writes one); v1 'completed' becomes DONE. Other v1 statuses keep their meaning where the
  lifecycle has one, else PROPOSED with the v1 status in `substate`.
- Legacy messages are stored as INFORM-shaped history with their original v1 state; they
  never wake anyone (spec 8.4: INFORM never wakes).
- `nanook` is Frank's wiring name (D-002) and maps to principal `frank`. The retired v1
  orchestrator ("supervisor", D-006) becomes an inactive service principal.
- One `migration.imported` event per run records counts and the id mapping digest.

Usage: python -m blimp.mesh.deck.import_v1 [--dsn DSN] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .board import next_id
from .events import append_event

AGENT_MAP = {"duke": "duke", "hermes": "hermes", "frank": "frank", "nanook": "frank", "logan": "logan",
             "supervisor": "svc-orchestrator-v1"}
STATE_MAP = {"pending": "PROPOSED", "completed": "DONE", "done": "DONE", "canceled": "CANCELED",
             "cancelled": "CANCELED", "failed": "FAILED", "blocked": "BLOCKED", "in_progress": "PROPOSED",
             "working": "PROPOSED"}
PRIORITY_SCORE = {"P0": 8, "P1": 5, "P2": 3, "P3": 1}


def _text_of(body: Any) -> str:
    """Pull the human text out of a v1 JSON-RPC body; fall back to compact JSON."""
    try:
        parts = body["params"]["message"]["parts"]
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("text")]
        if texts:
            return "\n".join(texts)
    except (KeyError, TypeError):
        pass
    return json.dumps(body, ensure_ascii=False)[:6000]


def run(dsn: str, dry_run: bool = False, out=print) -> dict[str, int]:
    stats = {"tasks": 0, "tasks_skipped": 0, "messages": 0, "messages_skipped": 0, "threads": 0}
    mapping: list[tuple[str, str, str]] = []
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO mesh.principals (id, kind, display_name, active)
                       VALUES ('svc-orchestrator-v1','service','v1 orchestrator (retired, D-006)', false)
                       ON CONFLICT (id) DO NOTHING""")
        cur.execute("SELECT source_id FROM mesh.import_map WHERE source='v1.tasks'")
        done_tasks = {r[0] for r in cur.fetchall()}
        v1_to_t: dict[str, str] = {}
        cur.execute("SELECT source_id, target_id FROM mesh.import_map WHERE source='v1.tasks'")
        v1_to_t.update(dict(cur.fetchall()))
        cur.execute("SELECT id, title, description, status, priority, assigned_to, parent_id, metadata, created_at, "
                    "updated_at, completed_at FROM public.tasks ORDER BY id")
        for (vid, title, desc, status, prio, assigned, parent, meta, created, updated, completed) in cur.fetchall():
            if str(vid) in done_tasks:
                stats["tasks_skipped"] += 1
                continue
            tid = next_id(cur, "T")
            owner = AGENT_MAP.get((assigned or "").lower())
            state = STATE_MAP.get((status or "").lower(), "PROPOSED")
            substate = None if (status or "").lower() in STATE_MAP else f"v1:{status}"
            contract = {"id": tid, "legacy": True, "v1_task_id": vid, "title": title, "v1_status": status,
                        "v1_priority": prio, "v1_assigned_to": assigned, "description": desc,
                        "note": "Imported from v1. No v2 contract yet: this task cannot become READY until one is proposed and passes lint."}
            short = title if len(title) <= 90 else title[:87] + "..."
            cur.execute(
                """INSERT INTO mesh.tasks (id, title, type, state, substate, owner, context_id, risk_tier, effort_class,
                   priority, contract, budget, created_by, created_at, updated_at, parent_id)
                   VALUES (%s,%s,'maintenance',%s,%s,%s,%s,'R1','S',%s,%s,%s,'frank',%s,%s,NULL)""",
                (tid, short, state, substate, owner, f"ctx-{tid}", PRIORITY_SCORE.get(prio, 0), Jsonb(contract),
                 Jsonb({"tokens": 0, "turns": 0}), created, updated),
            )
            cur.execute("INSERT INTO mesh.threads (context_id, kind, task_id) VALUES (%s,'task',%s)", (f"ctx-{tid}", tid))
            cur.execute("INSERT INTO mesh.import_map (source, source_id, target_id) VALUES ('v1.tasks', %s, %s)", (str(vid), tid))
            v1_to_t[str(vid)] = tid
            mapping.append(("v1.tasks", str(vid), tid))
            stats["tasks"] += 1
        # parent links, now that every task has its new id
        cur.execute("SELECT id, parent_id FROM public.tasks WHERE parent_id IS NOT NULL")
        for vid, parent in cur.fetchall():
            if str(parent) in v1_to_t and str(vid) in v1_to_t:
                cur.execute("UPDATE mesh.tasks SET parent_id=%s WHERE id=%s AND parent_id IS NULL", (v1_to_t[str(parent)], v1_to_t[str(vid)]))

        cur.execute("SELECT source_id FROM mesh.import_map WHERE source='v1.a2a_messages'")
        done_msgs = {r[0] for r in cur.fetchall()}
        cur.execute("SELECT id, task_id, parent_task_id, from_agent, to_agent, state, skill, body, created_at, acked_at, "
                    "delivered_at FROM public.a2a_messages ORDER BY id")
        for (mid, a2a_task, parent_task, frm, to, state, skill, body, created, acked, delivered) in cur.fetchall():
            if str(mid) in done_msgs:
                stats["messages_skipped"] += 1
                continue
            sender = AGENT_MAP.get((frm or "").lower())
            recipient = AGENT_MAP.get((to or "").lower())
            if not sender or not recipient:
                out(f"skip v1 message {mid}: unknown agent {frm!r} -> {to!r}")
                stats["messages_skipped"] += 1
                continue
            ctx = f"ctx-a2a-{a2a_task}"
            cur.execute("INSERT INTO mesh.threads (context_id, kind) VALUES (%s,'dm') ON CONFLICT DO NOTHING RETURNING context_id", (ctx,))
            if cur.fetchone():
                stats["threads"] += 1
            new_id = f"v1-a2a-{mid}"
            cur.execute(
                """INSERT INTO mesh.messages (id, context_id, sender, recipients, act, expects, priority, purpose, parts, refs,
                   created_at, delivered_at, read_at) VALUES (%s,%s,%s,%s,'INFORM','none','P3',%s,%s,%s,%s,%s,%s)""",
                (new_id, ctx, sender, [recipient], f"v1 A2A ({state})"[:160], Jsonb([{"text": _text_of(body)}]),
                 Jsonb({"legacy": True, "v1_id": mid, "v1_task_id": a2a_task, "v1_parent_task_id": parent_task,
                        "v1_state": state, "v1_skill": skill}),
                 created, delivered or created, acked),
            )
            cur.execute("INSERT INTO mesh.import_map (source, source_id, target_id) VALUES ('v1.a2a_messages', %s, %s)", (str(mid), new_id))
            mapping.append(("v1.a2a_messages", str(mid), new_id))
            stats["messages"] += 1

        digest = hashlib.sha256(json.dumps(mapping).encode()).hexdigest()
        if stats["tasks"] or stats["messages"]:
            append_event(cur, "frank", "migration.imported", {**stats, "mapping_sha256": digest}, "migration", "v1")
        out(json.dumps(stats))
        if dry_run:
            conn.rollback()
            out("dry run: rolled back, nothing changed")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import v1 tasks and A2A messages into the mesh board")
    ap.add_argument("--dsn", default=os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    run(a.dsn, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
