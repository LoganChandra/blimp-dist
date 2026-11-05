"""Event log: hash chain integrity, tamper detection, concurrent appends (spec §10.1, §14.7)."""
from __future__ import annotations

import threading

import psycopg

from blimp.mesh.deck.events import EventStore


def test_chain_verifies_after_appends(fresh_db):
    s = EventStore(fresh_db)
    for i in range(5):
        s.append("frank", "pulse.ok", {"i": i}, "pulse", "backup")
    assert s.verify_chain() == (True, None)


def test_timestamp_is_covered_by_hash(fresh_db):
    s = EventStore(fresh_db)
    s.append("frank", "pulse.ok", {"i": 1}, "pulse", "backup")
    s.append("frank", "pulse.ok", {"i": 2}, "pulse", "backup")
    with psycopg.connect(fresh_db, autocommit=True) as c:
        # the append-only RULE blocks UPDATE; a DBA bypass (dropping it) is what tampering looks like
        c.execute("DROP RULE events_no_update ON mesh.events")
        c.execute("UPDATE mesh.events SET ts = ts - interval '1 day' WHERE id = (SELECT min(id) FROM mesh.events)")
    ok, broken = s.verify_chain()
    assert not ok and broken is not None


def test_update_and_delete_are_ignored(fresh_db):
    s = EventStore(fresh_db)
    s.append("frank", "pulse.ok", {"i": 1}, "pulse", "backup")
    with psycopg.connect(fresh_db, autocommit=True) as c:
        c.execute("UPDATE mesh.events SET type='x'")
        c.execute("DELETE FROM mesh.events")
        assert c.execute("SELECT count(*), min(type) FROM mesh.events").fetchone() == (1, "pulse.ok")


def test_concurrent_appends_never_fork_the_chain(fresh_db):
    s = EventStore(fresh_db)
    errors = []

    def worker(n):
        try:
            for i in range(15):
                s.append("duke", "tool.called", {"w": n, "i": i}, "turn", f"TU-{n}")
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    with psycopg.connect(fresh_db) as c:
        n, distinct_prev = c.execute("SELECT count(*), count(DISTINCT prev_hash) FROM mesh.events").fetchone()
    assert n == 90 and distinct_prev == 90  # every event has a unique predecessor: a single line, no fork
    assert s.verify_chain() == (True, None)
