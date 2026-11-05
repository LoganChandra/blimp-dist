"""Discord Bridge, outbound half (spec §18.1, §8.7): board events -> Discord, exactly once.

Two stages, both deterministic, no LLM:

1. project(): read mesh.events after the cursor and turn them into mesh.outbox rows.
   - task.drafted          -> create the task's thread in #board, first post = the task card
   - task.state_changed,
     task.ledger_updated,
     task.claimed           -> EDIT the task card in place (no new post, no notification)
   - message.sent           -> post a message card in the task thread as the sender's persona
                               (INFORM and STATUS are folded into one "timeline" post that is
                               edited in place, spec 8.7); visibility=hq also summarizes in #hq;
                               visibility=private is never mirrored
   Every row has an idempotency key derived from the event, so re-projecting is harmless.

2. drain(): send pending rows through a Transport, record Discord's message id as proof of
   delivery, retry with backoff on failure. Edits wait until the message they edit exists.

The Transport is pluggable: DiscordTransport talks to the Discord REST API (bot token for
threads/edits, one webhook per agent persona for identity); tests use a fake. Nothing is
posted unless BRIDGE_LIVE=1: the default is a dry run that logs intended posts and leaves
rows pending (same safety rule as v1's discord_mirror.py).

Usage: python -m blimp.mesh.bridge.bridge [--once] [--backfill] [--dsn DSN]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import psycopg

from ..deck.board import Board
from ..deck.cards import approval_card, message_card, task_card

MYT = timezone(timedelta(hours=8), "MYT")
# the Boss's decision windows (spec 8.10, 9.10; G-10 default 09:30 and 17:30 MYT). Approval cards
# wait for the next window unless the request is urgent.
DECISION_WINDOWS = [tuple(map(int, w.split(":"))) for w in os.environ.get("BRIDGE_DECISION_WINDOWS", "09:30,17:30").split(",")]


def next_window(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(MYT)
    for days in (0, 1):
        for h, m in sorted(DECISION_WINDOWS):
            w = (now + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
            if w > now:
                return w.astimezone(timezone.utc)
    return now.astimezone(timezone.utc)

CARD_EVENTS = {"task.state_changed", "task.ledger_updated", "task.claimed", "task.ready", "lease.expired", "task.done"}
TIMELINE_ACTS = {"INFORM", "STATUS"}
MAX_ATTEMPTS = 8
DRAIN_LOCK_KEY = 7_406_120_002  # session advisory lock: only one drainer posts at a time


class Transport(Protocol):
    def create_thread(self, channel: str, title: str, content: str, persona: str, nonce: str | None = None) -> tuple[str, str]:
        """Returns (thread_id, first_message_id)."""

    def post(self, channel: str, thread_id: str | None, content: str, persona: str, nonce: str | None = None) -> str:
        """Returns message_id."""

    def edit(self, channel: str, thread_id: str | None, message_id: str, content: str, persona: str) -> None: ...


class DryRunTransport:
    """Logs what would be posted and refuses to pretend it was delivered."""

    def __init__(self, out=print):
        self.out = out

    def create_thread(self, channel, title, content, persona, nonce=None):
        self.out(f"[dry-run] thread in #{channel}: {title!r}\n{content}")
        raise DryRun()

    def post(self, channel, thread_id, content, persona, nonce=None):
        self.out(f"[dry-run] post #{channel}/{thread_id} as {persona}:\n{content}")
        raise DryRun()

    def edit(self, channel, thread_id, message_id, content, persona):
        self.out(f"[dry-run] edit {message_id} in #{channel}/{thread_id}:\n{content}")
        raise DryRun()


class DryRun(Exception):
    pass


def _nonce(key: str) -> str:
    """Discord dedupes a bot message with the same nonce for a few minutes (enforce_nonce), which
    closes the gap between a successful post and recording its id if the process dies there."""
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()[:25]


def _thread_title(task: dict) -> str:
    return f"{task['id']} · {task['title']}"[:100]  # Discord thread name limit


class Bridge:
    def __init__(self, dsn: str, transport: Transport | None = None, out=print):
        self.dsn = dsn
        self.board = Board(dsn)
        self.transport = transport or DryRunTransport(out)
        self.out = out

    # ── stage 1: project events into the outbox ────────────────────────────
    def backfill(self) -> int:
        """Mark everything already in the log as seen (run once before going live, so history
        is not posted retroactively)."""
        with psycopg.connect(self.dsn) as conn:
            last = conn.execute("SELECT COALESCE(max(id), 0) FROM mesh.events").fetchone()[0]
            conn.execute("UPDATE mesh.bridge_cursor SET last_event_id=%s, updated_at=now()", (last,))
        return last

    def project(self, batch: int = 500) -> int:
        made = 0
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT last_event_id FROM mesh.bridge_cursor FOR UPDATE")
            last = cur.fetchone()[0]
            cur.execute("SELECT id, actor, type, subject_type, subject_id, payload FROM mesh.events WHERE id > %s ORDER BY id LIMIT %s",
                        (last, batch))
            rows = cur.fetchall()
            for (eid, actor, etype, stype, sid, payload) in rows:
                cur.execute("SAVEPOINT ev")
                try:
                    made += self._project_one(cur, eid, actor, etype, stype, sid, payload or {})
                    cur.execute("RELEASE SAVEPOINT ev")
                except Exception as e:  # one bad event must never stop all Discord output
                    cur.execute("ROLLBACK TO SAVEPOINT ev")
                    self.out(f"[bridge] skipped event {eid} ({etype}): {e}")
                last = eid
            cur.execute("UPDATE mesh.bridge_cursor SET last_event_id=%s, updated_at=now()", (last,))
        return made

    def _enqueue(self, cur, key: str, op: str, channel: str, content: str, *, context_id=None, persona="deck",
                 edit_of_key=None, source_event_id=None) -> int:
        cur.execute(
            """INSERT INTO mesh.outbox (idempotency_key, op, channel, context_id, persona, content, edit_of_key, source_event_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING""",
            (key, op, channel, context_id, persona, content, edit_of_key, source_event_id),
        )
        return cur.rowcount

    def _card_for(self, cur, task_id: str) -> str:
        cur.execute("SELECT body FROM mesh.ledgers WHERE task_id=%s", (task_id,))
        row = cur.fetchone()
        return task_card(self.board._task_row(cur, task_id), row[0] if row else None)

    def _ensure_thread(self, cur, tid: str, eid: int) -> int:
        """Every task gets its thread, including tasks that never had a task.drafted event the
        Bridge saw (v1 imports, anything before a backfill). Idempotent by key."""
        cur.execute("SELECT 1 FROM mesh.tasks WHERE id=%s", (tid,))
        if not cur.fetchone():
            return 0
        return self._enqueue(cur, f"thread:{tid}", "thread_create", "board", self._card_for(cur, tid),
                             context_id=f"ctx-{tid}", source_event_id=eid)

    def _project_one(self, cur, eid, actor, etype, stype, sid, payload) -> int:
        if etype == "task.drafted":
            return self._ensure_thread(cur, payload["task_id"], eid)
        if etype in CARD_EVENTS and stype == "task":
            made = self._ensure_thread(cur, sid, eid)
            # A newer edit supersedes an older pending one: keep a single pending edit per card.
            cur.execute("UPDATE mesh.outbox SET status='skipped' WHERE op='edit' AND edit_of_key=%s AND status='pending'", (f"thread:{sid}",))
            return made + self._enqueue(cur, f"card:{sid}:{eid}", "edit", "board", self._card_for(cur, sid),
                                        context_id=f"ctx-{sid}", edit_of_key=f"thread:{sid}", source_event_id=eid)
        if etype == "approval.requested":
            ap = {**payload, "id": payload["approval_id"], "requested_by": actor}
            n = self._enqueue(cur, f"approval:{ap['id']}", "post", "approvals", approval_card(ap), persona="deck", source_event_id=eid)
            if n and not payload.get("urgent"):
                cur.execute("UPDATE mesh.outbox SET next_attempt_at=%s WHERE idempotency_key=%s", (next_window(), f"approval:{ap['id']}"))
            return n
        if etype in ("approval.granted", "approval.denied", "approval.expired", "approval.consumed"):
            verb = {"approval.granted": "approved by the Boss", "approval.denied": "rejected by the Boss",
                    "approval.expired": "expired", "approval.consumed": "executed"}[etype]
            note = f" · {payload['note']}" if payload.get("note") else ""
            return self._enqueue(cur, f"{etype}:{payload['approval_id']}", "post", "approvals",
                                 f"{payload['approval_id']} {verb}{note}", persona="deck", source_event_id=eid)
        if etype == "alert.raised" and payload.get("severity") in ("sev1", "sev2"):
            # A human must hear about sev1/sev2 even when D-008 keeps agents asleep.
            what = payload.get("job") or payload.get("kind") or "alert"
            detail = json.dumps(payload.get("detail") or payload.get("error") or {}, default=str)[:300]
            return self._enqueue(cur, f"alert:{eid}", "post", "hq",
                                 f"{payload['severity'].upper()} · {what} · {detail}", persona="frank", source_event_id=eid)
        if etype == "message.sent":
            m = payload.get("message") or {}
            sa = (m.get("metadata") or {}).get("https://blimp.my/a2a/ext/speech-act/v1") or {}
            vis = sa.get("visibility", "thread")
            if vis == "private":
                return 0
            tid = m.get("taskId")
            n = self._ensure_thread(cur, tid, eid) if tid else 0
            card = message_card({"act": sa.get("act"), "sender": sa.get("from"), "recipients": sa.get("to"),
                                 "purpose": sa.get("purpose"), "parts": m.get("parts")})
            if sa.get("act") in TIMELINE_ACTS:
                key = f"timeline:{tid}"
                cur.execute("SELECT content FROM mesh.outbox WHERE idempotency_key=%s", (key,))
                row = cur.fetchone()
                if not row:
                    n += self._enqueue(cur, key, "post", "board", f"Timeline\n{card}", context_id=f"ctx-{tid}", persona="deck", source_event_id=eid)
                else:
                    lines = (row[0] + "\n" + card).splitlines()
                    body = "\n".join(lines[-20:]) if len(lines) > 20 else "\n".join(lines)
                    if not body.startswith("Timeline"):
                        body = "Timeline\n" + body
                    cur.execute("UPDATE mesh.outbox SET content=%s WHERE idempotency_key=%s", (body, key))
                    cur.execute("UPDATE mesh.outbox SET status='skipped' WHERE op='edit' AND edit_of_key=%s AND status='pending'", (key,))
                    n += self._enqueue(cur, f"timeline:{tid}:{eid}", "edit", "board", body, context_id=f"ctx-{tid}",
                                       edit_of_key=key, source_event_id=eid)
            else:
                n += self._enqueue(cur, f"msg:{m.get('messageId')}", "post", "board", card, context_id=f"ctx-{tid}",
                                   persona=sa.get("from") or "deck", source_event_id=eid)
            if vis == "hq":
                n += self._enqueue(cur, f"hq:{m.get('messageId')}", "post", "hq", f"{tid} · {card}", persona=sa.get("from") or "deck",
                                   source_event_id=eid)
            return n
        return 0

    # ── stage 2: deliver ───────────────────────────────────────────────────
    def drain(self, limit: int = 50) -> dict[str, int]:
        stats = {"sent": 0, "failed": 0, "waiting": 0, "dry_run": 0, "orphaned": 0, "locked": 0}
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            # One drainer at a time (a manual --once next to the service must not double-post).
            if not conn.execute("SELECT pg_try_advisory_lock(%s)", (DRAIN_LOCK_KEY,)).fetchone()[0]:
                stats["locked"] = 1
                return stats
            try:
                return self._drain(conn, limit, stats)
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (DRAIN_LOCK_KEY,))

    def _wait(self, conn, oid: int, parent_key: str | None, stats: dict) -> None:
        """A row whose parent (thread or target message) is not sent yet: fail it if the parent
        failed for good, else look again later so it cannot clog the head of the queue."""
        parent = conn.execute("SELECT status FROM mesh.outbox WHERE idempotency_key=%s", (parent_key,)).fetchone() if parent_key else None
        if parent and parent[0] in ("failed", "skipped"):
            conn.execute("UPDATE mesh.outbox SET status='failed', last_error=%s WHERE id=%s", (f"parent {parent_key} {parent[0]}", oid))
            stats["orphaned"] += 1
        else:
            conn.execute("UPDATE mesh.outbox SET next_attempt_at=now() + interval '30 seconds' WHERE id=%s", (oid,))
            stats["waiting"] += 1

    def _drain(self, conn, limit: int, stats: dict) -> dict[str, int]:
        if True:
            rows = conn.execute(
                "SELECT id, idempotency_key, op, channel, context_id, persona, content, edit_of_key, attempts FROM mesh.outbox "
                "WHERE status='pending' AND next_attempt_at <= now() ORDER BY id LIMIT %s", (limit,)).fetchall()
            for (oid, key, op, channel, ctx, persona, content, edit_of, attempts) in rows:
                try:
                    thread_id = None
                    if ctx:
                        r = conn.execute("SELECT discord_thread_id FROM mesh.threads WHERE context_id=%s", (ctx,)).fetchone()
                        thread_id = r[0] if r else None
                    if op == "thread_create":
                        task = self.board.get_task(ctx.removeprefix("ctx-"))
                        tid, mid = self.transport.create_thread(channel, _thread_title(task), content, persona, nonce=_nonce(key))
                        conn.execute("UPDATE mesh.threads SET discord_thread_id=%s WHERE context_id=%s", (tid, ctx))
                        conn.execute("UPDATE mesh.outbox SET status='sent', sent_at=now(), discord_message_id=%s, discord_thread_id=%s, attempts=attempts+1 WHERE id=%s",
                                     (mid, tid, oid))
                    elif op == "post":
                        if ctx and channel == "board" and not thread_id:
                            self._wait(conn, oid, f"thread:{ctx.removeprefix('ctx-')}", stats)  # the thread is created first
                            continue
                        mid = self.transport.post(channel, thread_id if channel == "board" else None, content, persona, nonce=_nonce(key))
                        conn.execute("UPDATE mesh.outbox SET status='sent', sent_at=now(), discord_message_id=%s, discord_thread_id=%s, attempts=attempts+1 WHERE id=%s",
                                     (mid, thread_id, oid))
                    else:  # edit
                        target = conn.execute("SELECT discord_message_id FROM mesh.outbox WHERE idempotency_key=%s AND status='sent'", (edit_of,)).fetchone()
                        if not target:
                            self._wait(conn, oid, edit_of, stats)
                            continue
                        self.transport.edit(channel, thread_id, target[0], content, persona)
                        conn.execute("UPDATE mesh.outbox SET status='sent', sent_at=now(), discord_message_id=%s, attempts=attempts+1 WHERE id=%s",
                                     (target[0], oid))
                    stats["sent"] += 1
                except DryRun:
                    stats["dry_run"] += 1
                except Exception as e:  # network, rate limit, Discord 5xx
                    attempts += 1
                    delay = min(3600, 5 * 2 ** attempts)
                    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
                    conn.execute("UPDATE mesh.outbox SET attempts=%s, last_error=%s, status=%s, next_attempt_at=now() + make_interval(secs => %s) WHERE id=%s",
                                 (attempts, str(e)[:500], status, delay, oid))
                    stats["failed"] += 1
        return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Blimp Discord Bridge (outbound)")
    ap.add_argument("--dsn", default=os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    a = ap.parse_args(argv)
    transport = None
    if os.environ.get("BRIDGE_LIVE") == "1":
        from .discord_rest import DiscordTransport

        transport = DiscordTransport.from_env()
    b = Bridge(a.dsn, transport)
    if a.backfill:
        print(f"cursor set to event {b.backfill()}; history will not be posted")
        return 0
    while True:
        made = b.project()
        stats = b.drain()
        if a.once:
            print(json.dumps({"projected": made, **stats}))
            return 0
        time.sleep(2 if (made or stats["sent"]) else 5)  # SQL polling only: zero tokens


if __name__ == "__main__":
    sys.exit(main())
