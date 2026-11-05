"""Regression tests for the 2026-09-30 independent review of phase 1 (one per finding)."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from blimp.mesh.bridge.bridge import Bridge
from blimp.mesh.deck.board import Board, BoardError
from blimp.mesh.deck.cards import task_card
from blimp.mesh.deck.events import EventStore

from .test_board import LEDGER, contract, msg, ready_task
from .test_bridge import FakeDiscord, cycle

EXT = "https://blimp.my/a2a/ext/speech-act/v1"


@pytest.fixture()
def board(fresh_db):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    return b


# P0: a malformed ledger plan must never be accepted, and must never crash the Bridge
@pytest.mark.parametrize("plan", [["step 1", "step 2"], [{"what": "x", "status": "finished"}], [{"what": "x", "status": "done", "receipts": "RC-1"}], [None]])
def test_malformed_plan_rejected(board, plan):
    tid = ready_task(board)
    board.claim(tid, "duke")
    with pytest.raises(BoardError) as e:
        board.ledger_update(tid, "duke", {**LEDGER, "plan": plan})
    assert e.value.code == "ledger_invalid"


def test_card_tolerates_garbage_ledger():
    t = {"id": "T-1", "title": "x", "state": "WORKING", "owner": "duke", "contract": {}}
    for bad in (None, {"plan": ["a", 3, None]}, {"plan": "nope", "next_step": 5}, {"plan": [{"receipts": [1, "RC-1"]}]}):
        task_card(t, bad)  # never raises


def test_bridge_skips_an_unrenderable_event_and_keeps_going(board):
    fake = FakeDiscord()
    br = Bridge(board.dsn, fake, out=lambda *_: None)
    t1 = board.create_task(title="one", type="maintenance", created_by="frank")
    with psycopg.connect(board.dsn, autocommit=True) as c:  # a hostile/old event the projector cannot render
        EventStore(board.dsn).append("frank", "task.state_changed", {}, "task", "T-9999")
    t2 = board.create_task(title="two", type="maintenance", created_by="frank")
    cycle(br)
    titles = [x[1] for x in fake.threads]
    assert any("one" in x for x in titles) and any("two" in x for x in titles)


# P1: nobody takes over someone else's task by rewriting its contract
def test_propose_cannot_steal_ownership(board):
    t = board.create_task(title="for hermes", type="design", created_by="duke", mandate_id="M-0001")
    assert t["owner"] == "hermes"
    with pytest.raises(BoardError) as e:
        board.propose(t["id"], contract(t["id"], owner="duke", type="design"), "duke")
    assert e.value.code == "owner_change"
    with pytest.raises(BoardError) as e:
        board.propose(t["id"], contract(t["id"], owner="hermes", type="design"), "frank")  # unrelated agent
    assert e.value.code == "not_allowed"
    board.propose(t["id"], contract(t["id"], owner="duke", type="design"), "logan")  # the Boss may reassign
    assert board.get_task(t["id"])["owner"] == "duke"


def test_accept_refuses_a_contract_that_changed_after_review(board):
    t = board.create_task(title="x", type="build", created_by="hermes", mandate_id="M-0001")
    board.propose(t["id"], contract(t["id"]), "hermes")
    seen = board.get_task(t["id"])["version"]
    board.propose(t["id"], contract(t["id"], objective="something else entirely"), "hermes")
    with pytest.raises(BoardError) as e:
        board.make_ready(t["id"], "duke", reviewed_version=seen)
    assert e.value.code == "stale_version"


def test_unknown_owner_in_contract_is_a_clean_error(board):
    t = board.create_task(title="x", type="build", created_by="hermes", mandate_id="M-0001")
    with pytest.raises(BoardError) as e:
        board.propose(t["id"], contract(t["id"], reviewer="nobody"), "hermes")
    assert e.value.code == "unknown_principal"


# P1: a retried REQUEST-for-new-work creates exactly one task, even concurrently
def _new_work(task_ctx, mid):
    m = msg(task_ctx, act="REQUEST", text="REQUEST: add Telegram intake", expects="accept",
            reply_by=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            new_task={"title": "Telegram intake", "type": "build", "mandate_id": "M-0001"})
    m["taskId"], m["messageId"] = "new", mid
    return m


def test_retry_of_new_work_request_is_idempotent(board):
    tid = ready_task(board)
    mid = uuid.uuid4().hex
    first = board.send(_new_work((tid, f"ctx-{tid}"), mid))
    again = board.send(_new_work((tid, f"ctx-{tid}"), mid))
    assert again["duplicate"] and again["task_id"] == first["task_id"]
    assert [t["title"] for t in board.list_tasks()].count("Telegram intake") == 1


def test_concurrent_retries_create_one_task(board):
    tid = ready_task(board)
    mid = uuid.uuid4().hex
    out, errs = [], []

    def go():
        try:
            out.append(board.send(_new_work((tid, f"ctx-{tid}"), mid)))
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    assert [t["title"] for t in board.list_tasks()].count("Telegram intake") == 1
    assert sum(1 for r in out if not r.get("duplicate")) == 1


def test_failed_new_work_leaves_no_orphan(board):
    tid = ready_task(board)
    before = len(board.list_tasks())
    m = _new_work((tid, f"ctx-{tid}"), uuid.uuid4().hex)
    m["metadata"][EXT]["new_task"]["contract"] = contract("T-0000", reviewer="nobody")  # propose will fail
    with pytest.raises(BoardError):
        board.send(m)
    assert len(board.list_tasks()) == before


# P1: the Discord queue can't jam behind tasks that never got a thread; one drainer only
def test_imported_tasks_get_threads_and_queue_never_clogs(board):
    fake = FakeDiscord()
    br = Bridge(board.dsn, fake, out=lambda *_: None)
    for i in range(3):
        board.create_task(title=f"legacy {i}", type="maintenance", created_by="frank")
    br.backfill()  # as after the v1 import: history skipped, no task.drafted will be seen
    tids = [t["id"] for t in board.list_tasks()]
    for tid in tids:
        board.send(msg((tid, f"ctx-{tid}"), act="QUERY", text=f"QUERY: status of {tid}?"))
    cycle(br, 4)
    assert len(fake.threads) == 3 and len(fake.posts) == 3


def test_waiting_rows_are_rescheduled_and_orphans_fail(board):
    fake = FakeDiscord(fail_times=100)
    br = Bridge(board.dsn, fake, out=lambda *_: None)
    t = board.create_task(title="doomed", type="maintenance", created_by="frank")
    board.send(msg((t["id"], t["context_id"]), act="QUERY", text="QUERY: anyone?"))
    br.project()
    with psycopg.connect(board.dsn, autocommit=True) as c:
        c.execute("UPDATE mesh.outbox SET status='failed' WHERE op='thread_create'")
    s = br.drain()
    assert s["orphaned"] == 1
    with psycopg.connect(board.dsn) as c:
        assert c.execute("SELECT count(*) FROM mesh.outbox WHERE status='pending'").fetchone()[0] == 0


def test_only_one_drainer_at_a_time(board):
    br = Bridge(board.dsn, FakeDiscord(), out=lambda *_: None)
    with psycopg.connect(board.dsn, autocommit=True) as c:
        c.execute("SELECT pg_advisory_lock(7406120002)")
        assert br.drain()["locked"] == 1
        c.execute("SELECT pg_advisory_unlock(7406120002)")
    assert br.drain()["locked"] == 0


# P1: JSONB number normalization must not break the chain
@pytest.mark.parametrize("value", [1e16, -0.0, 1.10, 12345678901234567890, 0.1 + 0.2, {"b": 1, "a": [1.0, 2e-7]}])
def test_chain_survives_jsonb_normalization(board, value):
    EventStore(board.dsn).append("frank", "pulse.ok", {"v": value}, "pulse", "x")
    assert EventStore(board.dsn).verify_chain() == (True, None)


# P1: leases actually expire, and expiry is enforced on the holder's writes
def test_expired_lease_blocks_writes_and_renew_extends(board):
    tid = ready_task(board)
    board.claim(tid, "duke", ttl=timedelta(seconds=-1))
    with pytest.raises(BoardError) as e:
        board.ledger_update(tid, "duke", LEDGER)
    assert e.value.code == "lease_expired"
    with pytest.raises(BoardError) as e:
        board.transition(tid, "BLOCKED", "duke", "x")
    assert e.value.code == "lease_expired"


def test_tool_claims_last_the_contract_wall_clock(board):
    tid = ready_task(board, budget={"tokens": 100000, "turns": 5, "wall_clock_h": 6})
    exp = board.claim(tid, "duke")["lease_expires_at"]
    assert timedelta(hours=5.9) < exp - datetime.now(timezone.utc) <= timedelta(hours=6)
    tid2 = ready_task(board)
    exp2 = board.claim(tid2, "duke")["lease_expires_at"]
    assert timedelta(hours=3.9) < exp2 - datetime.now(timezone.utc) <= timedelta(hours=4)


def test_sweep_module_runs(board, monkeypatch, capsys):
    from blimp.mesh.ops import sweep
    monkeypatch.setenv("BLIMPG_DSN", board.dsn)
    tid = ready_task(board)
    board.claim(tid, "duke", ttl=timedelta(seconds=-1))
    sweep.main()
    assert json.loads(capsys.readouterr().out)["expired_leases"] == [tid]
