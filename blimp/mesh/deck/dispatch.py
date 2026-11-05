"""Deck dispatcher (spec §8.6 wake policy, §10.1 router/dispatcher, §10.5 crash recovery).

  route()    event log -> wake decisions -> coalesced, debounced, rate-limited turn requests
  claim()    a host takes the next due request for its agent (SELECT ... FOR UPDATE SKIP LOCKED)
  beat()     the host renews its claim while the turn runs (code, not LLM)
  finish()   done / failed
  recover()  requests whose host stopped beating go back to the queue; three crashes on the
             same request make it a poison turn: the task is BLOCKED and Frank is alerted

Wake mode (D-008, the Boss 2026-09-28: "yes. but only when i initiate it"): in
`logan_initiated` mode only the Boss's messages, his scheduled rituals and his approval
decisions wake an agent; everything else (a teammate's REQUEST, a verification result, an
unblocked task) is still delivered to the inbox and read at the agent's next turn, it just
does not start one. `spec` mode applies the full spec §8.6 rules. The mode is policy (R4).

Zero LLM. Idle cost: this file only runs SQL.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from .events import append_event
from .router import WakeEvent, evaluate_wakes

MYT = timezone(timedelta(hours=8), "MYT")
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
DEBOUNCE = {"P0": timedelta(0), "P1": timedelta(seconds=60), "P2": timedelta(minutes=10), "P3": timedelta(0)}
P3_WINDOWS = [(9, 0), (13, 0), (17, 0), (21, 0)]  # MYT (spec 8.6 [ASSUMPTION])
TURNS_PER_HOUR = {"duke": 12, "hermes": 10, "frank": 12}
HEARTBEAT_STALE = timedelta(minutes=3)
MAX_ATTEMPTS = 3
CREW = {"duke", "hermes", "frank"}
LOGAN_ACTS = {"approval.granted", "approval.denied"}


def wake_mode() -> str:
    return os.environ.get("BLIMP_WAKE_MODE", "logan_initiated")


def next_p3_window(now: datetime) -> datetime:
    local = now.astimezone(MYT)
    for days in (0, 1):
        for h, m in P3_WINDOWS:
            w = (local + timedelta(days=days)).replace(hour=h, minute=m, second=0, microsecond=0)
            if w >= local:
                return w.astimezone(timezone.utc)
    return now


def _initiator(etype: str, actor: str, payload: dict) -> str:
    if etype == "schedule.fired":
        return "schedule"
    if etype in LOGAN_ACTS:
        return "logan"
    return actor


class Dispatcher:
    def __init__(self, dsn: str | None = None, mode: str | None = None):
        self.dsn = dsn or os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")
        self.mode = mode or wake_mode()

    # ── routing ─────────────────────────────────────────────────────────────
    def route(self, batch: int = 500, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        stats = {"events": 0, "enqueued": 0, "coalesced": 0, "deferred": 0}
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT last_event_id FROM mesh.router_cursor FOR UPDATE")
            last = cur.fetchone()[0]
            cur.execute("SELECT id, type, actor, payload FROM mesh.events WHERE id > %s ORDER BY id LIMIT %s", (last, batch))
            rows = cur.fetchall()
            for eid, etype, actor, payload in rows:
                stats["events"] += 1
                last = eid
                payload = payload or {}
                initiator = _initiator(etype, actor, payload)
                for tr in evaluate_wakes([WakeEvent(eid, etype, actor, payload)]):
                    if tr.agent not in CREW:
                        continue
                    if tr.agent == actor and etype == "message.sent":
                        continue  # nobody wakes themselves
                    if self.mode == "logan_initiated" and initiator not in ("logan", "schedule"):
                        stats["deferred"] += 1
                        continue
                    made = self._enqueue(cur, tr.agent, tr.task_id, tr.priority, initiator, eid, now)
                    stats["enqueued" if made == "new" else "coalesced"] += 1
            cur.execute("UPDATE mesh.router_cursor SET last_event_id=%s, updated_at=now()", (last,))
            if stats["enqueued"] or stats["coalesced"]:
                cur.execute("NOTIFY blimp_turns")
        return stats

    def _enqueue(self, cur, agent, task_id, priority, initiator, event_id, now) -> str:
        if priority == "P3":
            not_before = next_p3_window(now)
        else:
            not_before = now + DEBOUNCE[priority]
        if priority != "P0":  # rate limit (spec 8.6); P0 (the Boss) is never throttled
            cur.execute("SELECT started_at FROM mesh.turn_requests WHERE agent=%s AND started_at > %s ORDER BY started_at",
                        (agent, now - timedelta(hours=1)))
            started = [r[0] for r in cur.fetchall()]
            if len(started) >= TURNS_PER_HOUR.get(agent, 10):
                not_before = max(not_before, started[0] + timedelta(hours=1))
        cur.execute(
            """INSERT INTO mesh.turn_requests (agent, task_id, priority, initiator, trigger_event_ids, not_before)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (agent, priority, COALESCE(task_id, '')) WHERE status = 'queued'
               DO UPDATE SET trigger_event_ids = array_append(mesh.turn_requests.trigger_event_ids, %s),
                             initiator = CASE WHEN EXCLUDED.initiator = 'logan' THEN 'logan' ELSE mesh.turn_requests.initiator END
               RETURNING (xmax = 0)""",
            (agent, task_id, priority, initiator, [event_id], not_before, event_id),
        )
        return "new" if cur.fetchone()[0] else "coalesced"

    # ── hosts ─────────────────────────────────────────────────────────────────
    def mode_for(self, cur, agent: str) -> str:
        cur.execute("SELECT scope, mode FROM mesh.system_modes WHERE scope IN ('global', %s)", (f"agent:{agent}",))
        modes = dict(cur.fetchall())
        for scope in ("global", f"agent:{agent}"):
            if modes.get(scope) in ("frozen", "paused"):
                return modes[scope]
        return modes.get("global") or modes.get(f"agent:{agent}") or "normal"

    def claim(self, agent: str, host: str, now: datetime | None = None) -> dict[str, Any] | None:
        """Next due request for this agent, most urgent first. One running turn per agent (WIP of
        the host); frozen/paused agents get nothing (spec 14.6)."""
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            if self.mode_for(cur, agent) in ("frozen", "paused"):
                return None
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("claim:" + agent,))
            cur.execute("SELECT 1 FROM mesh.turn_requests WHERE agent=%s AND status='running'", (agent,))
            if cur.fetchone():
                return None
            cur.execute(
                """UPDATE mesh.turn_requests SET status='running', host=%s, started_at=now(), heartbeat_at=now(), attempts=attempts+1, turn_id=NULL
                   WHERE id = (SELECT id FROM mesh.turn_requests WHERE agent=%s AND status='queued' AND not_before <= COALESCE(%s, now())
                               ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, id
                               FOR UPDATE SKIP LOCKED LIMIT 1)
                   RETURNING id, agent, task_id, priority, initiator, trigger_event_ids, attempts""",
                (host, agent, now),
            )
            row = cur.fetchone()
            if not row:
                return None
            keys = ["id", "agent", "task_id", "priority", "initiator", "trigger_event_ids", "attempts"]
            req = dict(zip(keys, row))
            append_event(cur, agent, "turn.requested", {"request_id": req["id"], "priority": req["priority"],
                                                        "initiator": req["initiator"], "task_id": req["task_id"]},
                         "turn_request", str(req["id"]), correlation_id=req["task_id"])
            return req

    def beat(self, request_id: int) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE mesh.turn_requests SET heartbeat_at=now() WHERE id=%s AND status='running'", (request_id,))

    def _requeue(self, cur, request_id: int, error: str, delay_s: int = 30) -> str:
        """Back to the queue, or poison after MAX_ATTEMPTS. If another queued request already exists
        for the same (agent, priority, task) (the Boss wrote again meanwhile), MERGE into it: its trigger
        events absorb this one's, and this row is dropped. Never raises on that conflict."""
        cur.execute("SELECT agent, priority, task_id, attempts, trigger_event_ids FROM mesh.turn_requests WHERE id=%s FOR UPDATE", (request_id,))
        row = cur.fetchone()
        if not row:
            return "missing"
        agent, priority, task_id, attempts, events = row
        if attempts >= MAX_ATTEMPTS:
            cur.execute("UPDATE mesh.turn_requests SET status='poison', host=NULL, last_error=%s WHERE id=%s", (error[:500], request_id))
            return "poison"
        cur.execute("SELECT id FROM mesh.turn_requests WHERE agent=%s AND priority=%s AND COALESCE(task_id,'')=COALESCE(%s,'') "
                    "AND status='queued' AND id<>%s FOR UPDATE", (agent, priority, task_id, request_id))
        other = cur.fetchone()
        if other:
            cur.execute("UPDATE mesh.turn_requests SET trigger_event_ids = %s || trigger_event_ids WHERE id=%s", (list(events or []), other[0]))
            cur.execute("UPDATE mesh.turn_requests SET status='dropped', host=NULL, last_error=%s WHERE id=%s",
                        (f"merged into request {other[0]} after: {error}"[:500], request_id))
            return "merged"
        cur.execute("UPDATE mesh.turn_requests SET status='queued', host=NULL, turn_id=NULL, last_error=%s, "
                    "not_before=now() + make_interval(secs => %s) WHERE id=%s", (error[:500], delay_s, request_id))
        return "queued"

    def finish(self, request_id: int, *, ok: bool, turn_id: str | None = None, error: str | None = None,
               retry: bool = True) -> str:
        """retry=False: the failed turn already had external effects, so it must not re-run
        (spec 10.5 "no work is repeated past the last receipt"): it fails and Frank is alerted."""
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            if ok:
                cur.execute("UPDATE mesh.turn_requests SET status='done', finished_at=now(), turn_id=%s WHERE id=%s", (turn_id, request_id))
                return "done"
            if not retry:
                cur.execute("UPDATE mesh.turn_requests SET status='failed', finished_at=now(), host=NULL, last_error=%s WHERE id=%s",
                            ((error or "")[:500], request_id))
                outcome = "failed"
            else:
                outcome = self._requeue(cur, request_id, error or "turn failed")
        if outcome in ("poison", "failed"):
            with psycopg.connect(self.dsn) as conn:
                self._poison_check(conn, request_id)
        return outcome

    def recover(self, now: datetime | None = None) -> dict[str, list[int]]:
        """Requests whose host stopped beating (crash, reboot, OOM) are re-queued (merged if needed);
        after three attempts they become poison turns (spec 10.5). One bad row never stops the rest."""
        now = now or datetime.now(timezone.utc)
        out = {"requeued": [], "poisoned": []}
        with psycopg.connect(self.dsn) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM mesh.turn_requests WHERE status='running' AND heartbeat_at < %s",
                                              (now - HEARTBEAT_STALE,)).fetchall()]
        for rid in ids:
            try:
                with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
                    outcome = self._requeue(cur, rid, "host stopped heartbeating (crash?)", delay_s=0)
                if outcome == "poison":
                    out["poisoned"].append(rid)
                    with psycopg.connect(self.dsn) as conn:
                        self._poison_check(conn, rid)
                elif outcome in ("queued", "merged"):
                    out["requeued"].append(rid)
            except Exception:  # pragma: no cover - logged by the daemon's output
                continue
        return out

    def _poison_check(self, conn, request_id: int) -> None:
        row = conn.execute("SELECT status, agent, task_id, last_error, initiator FROM mesh.turn_requests WHERE id=%s", (request_id,)).fetchone()
        if not row or row[0] not in ("poison", "failed"):
            return
        _, agent, task_id, err, initiator = row
        if initiator == "logan":
            # the Boss must never be left with an "on it" and then silence (spec 10.3 Rule #0).
            name = {"duke": "Duke", "frank": "Frank", "hermes": "Hermes"}.get(agent, agent)
            conn.execute("""INSERT INTO mesh.outbox (idempotency_key, op, channel, persona, content)
                            VALUES (%s,'post',%s,%s,%s) ON CONFLICT DO NOTHING""",
                         (f"turnfail:{request_id}", f"dm:{agent}", agent,
                          f"{name} could not finish your request{' on ' + task_id if task_id else ''}: the turn failed"
                          f"{' after 3 tries' if row[0] == 'poison' else ' after it had already acted, so it was not retried'}. "
                          f"Frank has been alerted. Last error: {(err or 'unknown')[:200]}"))
        with conn.cursor() as cur:
            if task_id:
                cur.execute("SELECT state FROM mesh.tasks WHERE id=%s FOR UPDATE", (task_id,))
                st = cur.fetchone()
                if st and st[0] == "WORKING":
                    cur.execute("UPDATE mesh.tasks SET state='BLOCKED', substate='poison_turn', lease_holder=NULL, lease_expires_at=NULL, "
                                "updated_at=now(), version=version+1 WHERE id=%s", (task_id,))
                    append_event(cur, "frank", "task.state_changed", {"from": "WORKING", "to": "BLOCKED", "reason": "poison turn",
                                                                      "substate": "poison_turn", "a2a_state": "WORKING"},
                                 "task", task_id, correlation_id=task_id)
            append_event(cur, "frank", "alert.raised", {"severity": "sev2", "kind": "poison_turn" if row[0] == "poison" else "turn_failed_after_effects",
                                                        "request_id": request_id,
                                                        "agent": agent, "task_id": task_id, "error": err},
                         "turn_request", str(request_id), correlation_id=task_id)

    # ── kill switch (spec 14.6) ───────────────────────────────────────────────
    def set_mode(self, scope: str, mode: str, set_by: str) -> None:
        if set_by not in ("logan", "frank"):
            raise PermissionError("only the Boss (or Frank, automatically on a spend spike) changes system modes")
        if set_by == "frank" and mode not in ("frozen", "paused"):
            raise PermissionError("Frank may only freeze or pause; thawing is the Boss's")
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO mesh.system_modes (scope, mode, set_by) VALUES (%s,%s,%s)
                           ON CONFLICT (scope) DO UPDATE SET mode=EXCLUDED.mode, set_by=EXCLUDED.set_by, set_at=now()""",
                        (scope, mode, set_by))
            append_event(cur, set_by, "system.freeze" if mode in ("frozen", "paused") else "system.thaw",
                         {"scope": scope, "mode": mode}, "system", scope)
