"""Dispatcher (spec §8.6, §10.5): D-008 wake gate, coalescing, debounce, rate limits, office hours,
single running turn per agent, crash recovery, poison turns, kill switch."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from blimp.mesh.deck.board import Board
from blimp.mesh.deck.dispatch import Dispatcher, next_p3_window
from blimp.mesh.deck.events import EventStore

from .test_board import msg, ready_task

EXT = "https://blimp.my/a2a/ext/speech-act/v1"


@pytest.fixture()
def world(fresh_db):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    tid = ready_task(b)
    return b, tid


def queued(dsn):
    with psycopg.connect(dsn) as c:
        return c.execute("SELECT agent, priority, initiator, cardinality(trigger_event_ids), status FROM mesh.turn_requests ORDER BY id").fetchall()


def logan_msg(b, tid, to="duke", text="QUERY: how is the intake going?"):
    return b.send(msg((tid, f"ctx-{tid}"), frm="logan", to=(to,), text=text))


def test_logan_message_wakes_p0_immediately(world):
    b, tid = world
    logan_msg(b, tid)
    s = Dispatcher(b.dsn, mode="logan_initiated").route()
    assert s["enqueued"] == 1 and queued(b.dsn) == [("duke", "P0", "logan", 1, "queued")]
    assert Dispatcher(b.dsn).claim("duke", "host-1")["priority"] == "P0"


def test_d008_agent_to_agent_does_not_wake(world):
    b, tid = world
    b.send(msg((tid, f"ctx-{tid}"), frm="hermes", to=("duke",)))
    s = Dispatcher(b.dsn, mode="logan_initiated").route()
    assert s["deferred"] == 1 and queued(b.dsn) == []
    assert b.inbox("duke")  # still delivered: read at the next the Boss-initiated turn


def test_spec_mode_agent_messages_wake_with_their_priority(world):
    b, tid = world
    b.send(msg((tid, f"ctx-{tid}"), frm="hermes", to=("duke",), priority="P1"))
    Dispatcher(b.dsn, mode="spec").route()
    assert queued(b.dsn)[0][:3] == ("duke", "P1", "hermes")


def test_logan_approval_decision_wakes_the_requester(world):
    b, tid = world
    EventStore(b.dsn).append("logan", "approval.granted", {"approval_id": "AP-0001", "requested_by": "frank"}, "approval", "AP-0001")
    Dispatcher(b.dsn, mode="logan_initiated").route()
    assert queued(b.dsn)[0][:3] == ("frank", "P1", "logan")


def test_coalescing_ten_events_one_turn(world):
    b, tid = world
    for i in range(10):
        logan_msg(b, tid, text=f"QUERY: point {i}?")
    Dispatcher(b.dsn).route()
    rows = queued(b.dsn)
    assert len(rows) == 1 and rows[0][3] == 10


def test_nobody_wakes_themselves(world):
    b, tid = world
    b.send(msg((tid, f"ctx-{tid}"), frm="duke", to=("duke",)))
    Dispatcher(b.dsn, mode="spec").route()
    assert queued(b.dsn) == []


def test_debounce_and_office_hours(world):
    b, tid = world
    now = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)  # 11:00 MYT
    d = Dispatcher(b.dsn, mode="spec")
    with psycopg.connect(b.dsn) as c, c.cursor() as cur:
        d._enqueue(cur, "duke", tid, "P2", "hermes", 1, now)
        d._enqueue(cur, "hermes", None, "P3", "schedule", 2, now)
    with psycopg.connect(b.dsn) as c:
        nb = dict(c.execute("SELECT priority, not_before FROM mesh.turn_requests").fetchall())
    assert nb["P2"] == now + timedelta(minutes=10)
    assert nb["P3"] == datetime(2026, 10, 1, 5, 0, tzinfo=timezone.utc)  # 13:00 MYT window
    assert d.claim("duke", "h", now=now) is None  # not due yet
    assert d.claim("duke", "h", now=now + timedelta(minutes=11))["priority"] == "P2"


def test_p3_windows():
    myt = timezone(timedelta(hours=8))
    assert next_p3_window(datetime(2026, 10, 1, 21, 30, tzinfo=myt)) == datetime(2026, 10, 2, 9, 0, tzinfo=myt)


def test_rate_limit_spares_logan(world):
    b, tid = world
    d = Dispatcher(b.dsn, mode="spec")
    now = datetime.now(timezone.utc)
    with psycopg.connect(b.dsn) as c:
        for i in range(12):
            c.execute("INSERT INTO mesh.turn_requests (agent, priority, initiator, status, started_at) VALUES ('duke','P2','hermes','done',%s)",
                      (now - timedelta(minutes=50 - i),))
    with psycopg.connect(b.dsn) as c, c.cursor() as cur:
        d._enqueue(cur, "duke", tid, "P2", "hermes", 1, now)
        d._enqueue(cur, "duke", tid, "P0", "logan", 2, now)
    with psycopg.connect(b.dsn) as c:
        nb = dict(c.execute("SELECT priority, not_before FROM mesh.turn_requests WHERE status='queued'").fetchall())
    assert nb["P2"] > now + timedelta(minutes=5) and nb["P0"] <= now + timedelta(seconds=1)


def test_one_running_turn_per_agent_even_when_racing(world):
    b, tid = world
    for p in ("P0", "P1"):
        with psycopg.connect(b.dsn) as c:
            c.execute("INSERT INTO mesh.turn_requests (agent, priority, initiator) VALUES ('duke', %s, 'logan')", (p,))
    d = Dispatcher(b.dsn)
    got = []
    ts = [threading.Thread(target=lambda: got.append(d.claim("duke", "h"))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(1 for g in got if g) == 1


def test_crash_recovery_then_poison(world):
    b, tid = world
    b.claim(tid, "duke")
    d = Dispatcher(b.dsn)
    with psycopg.connect(b.dsn) as c:
        c.execute("INSERT INTO mesh.turn_requests (agent, task_id, priority, initiator) VALUES ('duke', %s, 'P0', 'logan')", (tid,))
    for attempt in range(1, 4):
        req = d.claim("duke", f"host-{attempt}")
        assert req and req["attempts"] == attempt
        with psycopg.connect(b.dsn) as c:  # the host dies: no more heartbeats
            c.execute("UPDATE mesh.turn_requests SET heartbeat_at = now() - interval '10 minutes'")
        r = d.recover()
        with psycopg.connect(b.dsn) as c:
            c.execute("UPDATE mesh.turn_requests SET not_before = now() - interval '1 second' WHERE status='queued'")
    assert r["poisoned"] == [req["id"]]
    t = b.get_task(tid)
    assert t["state"] == "BLOCKED" and t["substate"] == "poison_turn"
    alerts = [e for e in EventStore(b.dsn).events_for_subject("turn_request", str(req["id"])) if e["type"] == "alert.raised"]
    assert alerts and alerts[0]["payload"]["kind"] == "poison_turn"


def test_heartbeat_keeps_a_long_turn_alive(world):
    b, tid = world
    d = Dispatcher(b.dsn)
    with psycopg.connect(b.dsn) as c:
        c.execute("INSERT INTO mesh.turn_requests (agent, priority, initiator) VALUES ('duke','P0','logan')")
    req = d.claim("duke", "h")
    d.beat(req["id"])
    assert d.recover()["requeued"] == []


def test_failed_turn_retries_then_poisons(world):
    b, tid = world
    d = Dispatcher(b.dsn)
    with psycopg.connect(b.dsn) as c:
        c.execute("INSERT INTO mesh.turn_requests (agent, priority, initiator) VALUES ('duke','P0','logan')")
    for _ in range(3):
        with psycopg.connect(b.dsn) as c:
            c.execute("UPDATE mesh.turn_requests SET not_before = now() - interval '1 second' WHERE status='queued'")
        req = d.claim("duke", "h")
        d.finish(req["id"], ok=False, error="model API 529")
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT status FROM mesh.turn_requests").fetchone()[0] == "poison"


def test_kill_switch(world):
    b, tid = world
    d = Dispatcher(b.dsn)
    with psycopg.connect(b.dsn) as c:
        c.execute("INSERT INTO mesh.turn_requests (agent, priority, initiator) VALUES ('duke','P0','logan')")
    with pytest.raises(PermissionError):
        d.set_mode("global", "frozen", "duke")
    d.set_mode("global", "frozen", "logan")
    assert d.claim("duke", "h") is None
    with pytest.raises(PermissionError):
        d.set_mode("global", "normal", "frank")  # only the Boss thaws
    d.set_mode("global", "normal", "logan")
    d.set_mode("agent:duke", "paused", "frank")
    assert d.claim("duke", "h") is None
    d.set_mode("agent:duke", "normal", "logan")
    assert d.claim("duke", "h") is not None


def test_router_is_idempotent(world):
    b, tid = world
    logan_msg(b, tid)
    d = Dispatcher(b.dsn)
    d.route()
    d.route()
    assert len(queued(b.dsn)) == 1
