"""v1 -> board import (spec §15 migration mapping): mapping, idempotence, nothing invented."""
from __future__ import annotations

import json

import psycopg
import pytest

from blimp.mesh.deck import import_v1
from blimp.mesh.deck.board import Board, BoardError
from blimp.mesh.deck.events import EventStore

from .conftest import MESH, TEST_DSN


@pytest.fixture()
def v1(fresh_db):
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS public.tasks, public.a2a_messages CASCADE")
        c.execute("DROP SEQUENCE IF EXISTS public.tasks_id_seq, public.a2a_messages_id_seq CASCADE")
        c.execute((MESH / "tests" / "fixtures" / "v1_schema.sql").read_text())
        c.execute("""INSERT INTO public.tasks (id, title, status, priority, assigned_to, parent_id) VALUES
            (1, 'Get first paying client', 'pending', 'P1', 'logan', NULL),
            (2, 'Validate LiveKit path', 'completed', 'P1', 'hermes', NULL),
            (3, 'Product templates', 'pending', 'P1', 'nanook', 1),
            (4, %s, 'weird-status', 'P2', 'someone-else', NULL)""", ("x" * 120,))
        body = {"method": "message/send", "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "Dual-agent voice room built."}]}}}
        c.execute("""INSERT INTO public.a2a_messages (id, task_id, from_agent, to_agent, state, body, acked_at) VALUES
            (61, '44', 'hermes', 'duke', 'submitted', %s, now()),
            (62, '44', 'duke', 'hermes', 'completed', %s, NULL),
            (63, '45', 'supervisor', 'duke', 'completed', %s, NULL),
            (64, '46', 'other-agent', 'duke', 'completed', %s, NULL)""",
                  (json.dumps(body), json.dumps({"x": 1}), json.dumps(body), json.dumps(body)))
    return TEST_DSN


def test_import_maps_and_is_idempotent(v1):
    s = import_v1.run(v1, out=lambda *_: None)
    assert s["tasks"] == 4 and s["messages"] == 3 and s["messages_skipped"] == 1 and s["threads"] == 2
    b = Board(v1)
    rows = {t["title"][:10]: t for t in b.list_tasks()}
    assert rows["Get first "]["state"] == "PROPOSED" and rows["Get first "]["owner"] == "logan"
    assert rows["Validate L"]["state"] == "DONE"
    assert rows["Product te"]["owner"] == "frank"  # nanook is Frank's wiring name
    long = [t for t in b.list_tasks() if t["title"].startswith("xxx")][0]
    assert len(long["title"]) == 90 and long["owner"] is None
    full = b.get_task(long["id"])
    assert full["contract"]["legacy"] is True and full["contract"]["v1_status"] == "weird-status"
    child = b.get_task(rows["Product te"]["id"])
    assert child["contract"]["v1_task_id"] == 3
    with psycopg.connect(v1) as c:
        parent = c.execute("SELECT parent_id FROM mesh.tasks WHERE id=%s", (child["id"],)).fetchone()[0]
        assert parent == rows["Get first "]["id"]
        m = c.execute("SELECT act, sender, recipients, parts, read_at IS NOT NULL, refs->>'v1_state' FROM mesh.messages WHERE id='v1-a2a-61'").fetchone()
        assert m[0] == "INFORM" and m[1] == "hermes" and m[2] == ["duke"] and m[3][0]["text"] == "Dual-agent voice room built." and m[4] and m[5] == "submitted"
        assert c.execute("SELECT sender FROM mesh.messages WHERE id='v1-a2a-63'").fetchone()[0] == "svc-orchestrator-v1"
    again = import_v1.run(v1, out=lambda *_: None)
    assert again["tasks"] == 0 and again["messages"] == 0 and again["tasks_skipped"] == 4
    assert EventStore(v1).verify_chain() == (True, None)


def test_imported_tasks_cannot_skip_to_ready(v1):
    import_v1.run(v1, out=lambda *_: None)
    b = Board(v1)
    t = [t for t in b.list_tasks() if t["state"] == "PROPOSED" and t["owner"] == "logan"][0]
    with pytest.raises(BoardError) as e:
        b.make_ready(t["id"], "logan")
    assert e.value.code == "lint_failed"


def test_dry_run_changes_nothing(v1):
    import_v1.run(v1, dry_run=True, out=lambda *_: None)
    with psycopg.connect(v1) as c:
        assert c.execute("SELECT count(*) FROM mesh.tasks").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM mesh.events").fetchone()[0] == 0
