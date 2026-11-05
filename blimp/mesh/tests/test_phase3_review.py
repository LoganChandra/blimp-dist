"""Regression tests for the 2026-09-30 review of phase 3 (one block per finding)."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import psycopg
import pytest

from blimp.mesh.deck.board import Board
from blimp.mesh.deck.dispatch import Dispatcher
from blimp.mesh.hosts.claude_host import Host, claude_runner
from blimp.mesh.toolgate.adapters import build
from blimp.mesh.toolgate.checks import CheckNotRunnable, CheckRunner
from blimp.mesh.toolgate.gate import Toolgate

from .conftest import mkwt
from .test_board import contract, msg
from .test_host import fake


@pytest.fixture()
def world(fresh_db):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.propose(t["id"], contract(t["id"], worktree=mkwt(t["id"]), capabilities=["repo.read", "repo.worktree"]), "hermes")
    b.make_ready(t["id"], "duke")
    b.claim(t["id"], "duke")
    return b, t["id"]


def logan(b, tid, text="QUERY: status?"):
    b.send(msg((tid, f"ctx-{tid}"), frm="logan", to=("duke",), text=text))
    Dispatcher(b.dsn).route()


# P0: repo tools and the sandbox obey the same worktree roots as checks
def test_repo_tools_refuse_a_contract_pointing_at_home(world, monkeypatch):
    b, tid = world
    with psycopg.connect(b.dsn, autocommit=True) as c:  # a contract that slipped past lint (or was edited in the DB)
        c.execute("""UPDATE mesh.tasks SET contract = jsonb_set(contract, '{worktree}', '"/home/user"') WHERE id=%s""", (tid,))
    g = Toolgate(b.dsn, build(b.dsn))
    r = g.call(g.open_turn("duke", tid), "duke", "repo.read", {"path": ".ssh/id_rsa"})
    assert not r.allowed and "outside the allowed roots" in r.reason


def test_symlink_out_of_the_worktree_is_refused(world):
    b, tid = world
    wt = Path(b.get_task(tid)["contract"]["worktree"])
    (wt / "escape").symlink_to("/etc")
    g = Toolgate(b.dsn, build(b.dsn))
    r = g.call(g.open_turn("duke", tid), "duke", "repo.read", {"path": "escape/hostname"})
    assert not r.allowed and "outside the task worktree" in r.reason


# P0: a failed turn plus another message from the Boss never jams the agent or crashes the kernel
def test_requeue_merges_into_a_waiting_request(world):
    b, tid = world
    d = Dispatcher(b.dsn)
    logan(b, tid, "QUERY: first?")
    req = d.claim("duke", "h")
    logan(b, tid, "QUERY: second, while the first is running?")
    assert d.finish(req["id"], ok=False, error="model 529") == "merged"
    with psycopg.connect(b.dsn) as c:
        rows = c.execute("SELECT status, cardinality(trigger_event_ids) FROM mesh.turn_requests ORDER BY id").fetchall()
    assert rows[0][0] == "dropped" and rows[1] == ("queued", 2)  # nothing lost, nothing stuck


def test_recover_merges_too_and_never_raises(world):
    b, tid = world
    d = Dispatcher(b.dsn)
    logan(b, tid, "QUERY: first?")
    req = d.claim("duke", "dead-host")
    logan(b, tid, "QUERY: second?")
    with psycopg.connect(b.dsn) as c:
        c.execute("UPDATE mesh.turn_requests SET heartbeat_at = now() - interval '10 minutes' WHERE id=%s", (req["id"],))
    assert d.recover()["requeued"] == [req["id"]]
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT count(*) FROM mesh.turn_requests WHERE status='running'").fetchone()[0] == 0


# P0: the host runs claude in the SAME directory every turn, so --resume finds the session
def test_host_cwd_is_stable_per_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("BLIMP_HOST_CWD", str(tmp_path / "hosts"))
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(f"""#!/bin/sh
pwd >> {tmp_path}/cwds
echo '{{"type":"result","is_error":false,"result":"ok","session_id":"s","num_turns":1,"usage":{{}}}}'
""")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    run = claude_runner("duke", claude=str(fake_claude))
    for resume in (False, True):
        run({"dsn": "x", "turn_id": "TU-1", "system_prompt": "", "user_prompt": "", "session_id": "s", "resume": resume, "wall_clock_s": 10})
    cwds = (tmp_path / "cwds").read_text().split()
    assert cwds[0] == cwds[1] == str(tmp_path / "hosts" / "duke") and not list((tmp_path / "hosts" / "duke").iterdir())


# P1: Hermes calls bind to a turn only with that turn's key (tested in test_host); stale turn ids are cleared
def test_claim_clears_a_stale_turn_id(world):
    b, tid = world
    d = Dispatcher(b.dsn)
    logan(b, tid)
    with psycopg.connect(b.dsn) as c:
        c.execute("UPDATE mesh.turn_requests SET turn_id='TU-OLD'")
    d.claim("duke", "h")
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT turn_id FROM mesh.turn_requests").fetchone()[0] is None


# P1: Rule #0 acks once per request, not once per attempt
def test_rule0_ack_once_per_request(world):
    b, tid = world
    logan(b, tid)
    host = Host("duke", b.dsn, runner=fake(ok=False, reply=False, ledger=False))
    for _ in range(3):
        with psycopg.connect(b.dsn) as c:
            c.execute("UPDATE mesh.turn_requests SET not_before = now() - interval '1 second' WHERE status='queued'")
        host.run_once()
    with psycopg.connect(b.dsn) as c:
        acks = c.execute("SELECT count(*) FROM mesh.outbox WHERE idempotency_key LIKE 'rule0:%'").fetchone()[0]
        fails = c.execute("SELECT count(*) FROM mesh.outbox WHERE idempotency_key LIKE 'turnfail:%'").fetchone()[0]
    assert acks == 1 and fails == 1  # one "on it", then one honest "could not finish"


# P1: no alert storm from a long-dead probe; sev1/sev2 reach the Boss through the Bridge
def test_long_dead_probe_alerts_once(fresh_db):
    from blimp.mesh.ops import health as h
    bad = [h.Result("hindsight", False, {}, "sev3")]
    h.record(fresh_db, [h.Result("hindsight", True, {})])
    h.record(fresh_db, bad)
    with psycopg.connect(fresh_db) as c:  # it has been failing for two days
        c.execute("UPDATE mesh.pulses SET last_ok_at = now() - interval '2 days' WHERE job='hindsight'")
    for _ in range(5):
        h.record(fresh_db, bad)
    with psycopg.connect(fresh_db) as c:
        assert c.execute("SELECT count(*) FROM mesh.events WHERE type IN ('alert.raised','pulse.missed')").fetchone()[0] == 1


def test_sev2_alert_is_posted_to_logan(fresh_db):
    from blimp.mesh.bridge.bridge import Bridge
    from blimp.mesh.ops import health as h

    from .test_bridge import FakeDiscord, cycle
    h.record(fresh_db, [h.Result("site:https://yourproduct.example", False, {"status": 502}, "sev2")])
    fake_d = FakeDiscord()
    cycle(Bridge(fresh_db, fake_d, out=lambda *_: None))
    assert any(p[0] == "hq" and p[2].startswith("SEV2 · site:https://yourproduct.example") for p in fake_d.posts)


# P1: unread teammate messages are in the next turn's pack
def test_deferred_messages_reach_the_next_turn(world):
    b, tid = world
    b.send(msg((tid, f"ctx-{tid}"), frm="hermes", to=("duke",), text="QUERY: retention period for raw payloads?"))
    Dispatcher(b.dsn).route()  # D-008: does not wake Duke
    logan(b, tid, "QUERY: anything blocking you?")
    seen = []
    Host("duke", b.dsn, runner=fake(seen=seen)).run_once()
    pack = seen[0]["system_prompt"]
    assert "retention period for raw payloads?" in pack and "Unread messages" in pack
    assert b.inbox("duke") == []


# P1: command checks fail closed without a sandbox
def test_command_checks_refuse_host_execution_without_sandbox(fresh_db, monkeypatch):
    monkeypatch.delenv("BLIMP_SANDBOX", raising=False)
    b = Board(fresh_db)
    b.record_mandate("x", "t")
    t = b.create_task(title="t", type="build", created_by="hermes", mandate_id="M-0001")
    b.propose(t["id"], contract(t["id"], worktree=mkwt(t["id"]),
                                acceptance_checks=[{"id": "c1", "kind": "cmd", "cmd": "true", "expected": "exit 0"}]), "hermes")
    b.approve_checks(t["id"], "logan")
    b.make_ready(t["id"], "duke")
    with pytest.raises(CheckNotRunnable) as e:
        CheckRunner(fresh_db).run(t["id"], "c1")
    assert "need the sandbox" in str(e.value)
