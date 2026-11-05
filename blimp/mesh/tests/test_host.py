"""Agent host (spec §10.3, §10.4): the turn lifecycle, Rule #0, manifests, sessions, crash recovery.
The model is a fake runner that uses the real Toolgate the way Claude would (through the gate)."""
from __future__ import annotations

import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest

from blimp.mesh.deck.board import Board
from blimp.mesh.deck.dispatch import Dispatcher
from blimp.mesh.deck.events import EventStore
from blimp.mesh.hosts.claude_host import Host, claude_runner
from blimp.mesh.toolgate.adapters import build
from blimp.mesh.toolgate.gate import Toolgate

from .test_board import LEDGER, contract, msg


@pytest.fixture()
def world(fresh_db, tmp_path):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    t = b.create_task(title="Intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.propose(t["id"], contract(t["id"]), "hermes")
    b.make_ready(t["id"], "duke")
    b.claim(t["id"], "duke")
    return b, t["id"]


def logan_says(b, tid, text="QUERY: status?"):
    b.send(msg((tid, f"ctx-{tid}"), frm="logan", to=("duke",), text=text))
    Dispatcher(b.dsn).route()


def fake(reply=True, ledger=True, ok=True, seen=None):
    def run(req):
        if seen is not None:
            seen.append(req)
        g = Toolgate(req["dsn"], build(req["dsn"]))
        if reply:
            g.call(req["turn_id"], "duke", "comms.reply_discord", {"text": "Intake: step 2 of 3 done."})
        if ledger and req.get("task_id"):
            g.call(req["turn_id"], "duke", "board.ledger_update", {"ledger": LEDGER})
        return {"ok": ok, "session_id": req["session_id"], "usage": {"input_tokens": 1200, "output_tokens": 80},
                "num_turns": 3, "error": None if ok else "model overloaded"}
    return run


def events(dsn, t):
    with psycopg.connect(dsn) as c:
        return [r[0] for r in c.execute("SELECT type FROM mesh.events WHERE type LIKE %s ORDER BY id", (t,)).fetchall()]


def test_full_turn(world):
    b, tid = world
    logan_says(b, tid, "QUERY: how is the intake going?")
    seen = []
    out = Host("duke", b.dsn, runner=fake(seen=seen)).run_once()
    assert out["ok"]
    req = seen[0]
    assert "how is the intake going?" in req["system_prompt"] and "<L0>" in req["system_prompt"].split("\n")[0]
    assert "reply to him with comms_reply_discord" in req["user_prompt"]
    with psycopg.connect(b.dsn) as c:
        turn = c.execute("SELECT agent, task_id, prompt_manifest, input_tokens, outcome FROM mesh.turns").fetchone()
        assert turn[0] == "duke" and turn[1] == tid and turn[3] == 1200 and turn[4] == "completed"
        layers = [m["layer"] for m in turn[2]]
        assert layers[:3] == ["L0", "L1", "L2"] and "L7" in layers  # load manifest (fixes L-017)
        assert c.execute("SELECT status FROM mesh.turn_requests").fetchone()[0] == "done"
        assert c.execute("SELECT count(*) FROM mesh.outbox WHERE channel='dm:duke'").fetchone()[0] == 1
    assert b.inbox("duke") == []  # included in the pack = read
    assert "rule0.miss" not in events(b.dsn, "rule0%")


def test_rule0_auto_ack_when_the_agent_did_not_reply(world):
    b, tid = world
    logan_says(b, tid)
    Host("duke", b.dsn, runner=fake(reply=False)).run_once()
    assert events(b.dsn, "rule0%") == ["rule0.miss"]
    with psycopg.connect(b.dsn) as c:
        text = c.execute("SELECT content FROM mesh.outbox WHERE idempotency_key LIKE 'rule0:%'").fetchone()[0]
    assert text.startswith(f"Duke is on it: {tid}. Update by ") and text.endswith(" MYT.")
    import re
    from datetime import datetime, timedelta, timezone
    hh, mm = map(int, re.search(r"Update by (\d\d):(\d\d) MYT", text).groups())
    now = datetime.now(timezone(timedelta(hours=8)))
    promised = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if promised < now - timedelta(minutes=1):
        promised += timedelta(days=1)
    assert timedelta(minutes=20) <= promised - now <= timedelta(minutes=31)  # a future time, in the Boss's clock


def test_missing_ledger_is_flagged(world):
    b, tid = world
    logan_says(b, tid)
    Host("duke", b.dsn, runner=fake(ledger=False)).run_once()
    assert events(b.dsn, "turn.ledger_missing") == ["turn.ledger_missing"]


def test_sessions_resume_per_task(world):
    b, tid = world
    seen = []
    host = Host("duke", b.dsn, runner=fake(seen=seen))
    for i in range(2):
        logan_says(b, tid, f"QUERY: {i}?")
        host.run_once()
    assert seen[0]["resume"] is False and seen[1]["resume"] is True and seen[0]["session_id"] == seen[1]["session_id"]


def test_failed_turn_with_effects_is_not_retried(world):
    """It already replied to the Boss: re-running would repeat that (spec 10.5)."""
    b, tid = world
    logan_says(b, tid)
    Host("duke", b.dsn, runner=fake(ok=False)).run_once()
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT status FROM mesh.turn_requests").fetchone()[0] == "failed"
        notice = c.execute("SELECT content FROM mesh.outbox WHERE idempotency_key LIKE 'turnfail:%'").fetchone()[0]
    assert notice.startswith("Duke could not finish your request") and "not retried" in notice


def test_failed_model_call_requeues_then_poisons(world):
    b, tid = world
    logan_says(b, tid)
    host = Host("duke", b.dsn, runner=fake(ok=False, reply=False))
    for _ in range(3):
        with psycopg.connect(b.dsn) as c:
            c.execute("UPDATE mesh.turn_requests SET not_before = now() - interval '1 second' WHERE status='queued'")
        host.run_once()
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT status FROM mesh.turn_requests").fetchone()[0] == "poison"
    assert b.get_task(tid)["state"] == "BLOCKED"


def test_crashed_host_turn_resumes_on_another_host(world):
    """Phase 3 exit: agents resume mid-task after a forced crash."""
    b, tid = world
    logan_says(b, tid)
    d = Dispatcher(b.dsn)
    req = d.claim("duke", "host-that-dies")  # the host dies mid-turn: no finish, no heartbeat
    with psycopg.connect(b.dsn) as c:
        c.execute("UPDATE mesh.turn_requests SET heartbeat_at = now() - interval '10 minutes', not_before = now() - interval '1 second'")
    assert d.recover()["requeued"] == [req["id"]]
    with psycopg.connect(b.dsn) as c:
        c.execute("UPDATE mesh.turn_requests SET not_before = now() - interval '1 second'")
    out = Host("duke", b.dsn, runner=fake()).run_once()
    assert out["ok"]
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT status, attempts FROM mesh.turn_requests").fetchone() == ("done", 2)


def test_turn_without_lease_is_a_chat_turn(world):
    b, tid = world
    b.transition(tid, "PARKED", "duke", "later")
    logan_says(b, tid)
    seen = []
    Host("duke", b.dsn, runner=fake(seen=seen)).run_once()
    assert seen[0]["task_id"] is None


def test_frozen_system_runs_nothing(world):
    b, tid = world
    logan_says(b, tid)
    Dispatcher(b.dsn).set_mode("global", "frozen", "logan")
    assert Host("duke", b.dsn, runner=fake()).run_once() is None


def test_claude_runner_argv_and_parsing(tmp_path):
    """The real runner, against a fake `claude` binary that records its argv."""
    fake_claude = tmp_path / "claude"
    log = tmp_path / "argv.txt"
    fake_claude.write_text(f"""#!/bin/sh
printf '%s\\n' "$@" > {log}
echo '{{"type":"result","is_error":false,"result":"done","session_id":"s-1","num_turns":2,"total_cost_usd":0.2,
"usage":{{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":3,"cache_creation_input_tokens":4}}}}'
""")
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)
    run = claude_runner("duke", model="sonnet", claude=str(fake_claude))
    out = run({"dsn": "postgresql://x", "turn_id": "TU-0001", "system_prompt": "<L0>...</L0>", "user_prompt": "go",
               "session_id": "s-1", "resume": True, "wall_clock_s": 30})
    argv = log.read_text().split("\n")
    assert out == {"ok": True, "session_id": "s-1", "result": "done", "error": None, "list_price_usd": 0.2, "num_turns": 2, "model": "sonnet",
                   "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 3, "cache_write_tokens": 4}}
    for flag in ("--strict-mcp-config", "--mcp-config", "--append-system-prompt-file", "--resume", "--allowedTools"):
        assert flag in argv
    assert argv[argv.index("--tools") + 1] == ""  # no built-in tools: Toolgate is the only surface
    assert argv[argv.index("--setting-sources") + 1] == ""  # no user/project settings or hooks leak in


def test_claude_runner_reports_garbage_output(tmp_path):
    bad = tmp_path / "claude"
    bad.write_text("#!/bin/sh\necho 'Error: not logged in' >&2\nexit 1\n")
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    out = claude_runner("duke", claude=str(bad))({"dsn": "x", "turn_id": "TU-1", "system_prompt": "", "user_prompt": "", "session_id": "s",
                                                   "resume": False, "wall_clock_s": 10})
    assert out["ok"] is False and "not logged in" in out["error"]


# ── Hermes adapter ───────────────────────────────────────────────────────────
def test_hermes_turn_via_signed_webhook_and_done_signal(fresh_db):
    import hashlib
    import hmac
    import io
    import json as _json

    from blimp.mesh.hosts.hermes_host import webhook_runner
    from blimp.mesh.toolgate.adapters import build

    b = Board(fresh_db)
    b.record_mandate("design intake", "t")
    t = b.create_task(title="Design", type="design", created_by="logan", mandate_id="M-0001", owner="hermes")
    b.send(msg((t["id"], t["context_id"]), frm="logan", to=("hermes",), text="QUERY: options for the intake?"))
    Dispatcher(fresh_db).route()
    posted = []

    def opener(req, timeout=15):
        posted.append(req)
        # the gateway runs Hermes; Hermes uses Toolgate (bound to the kernel's current turn) and signals done
        from blimp.mesh.toolgate.mcp_server import make_server
        import asyncio
        import re as _re
        key = _re.search(r'turn_key="([^"]+)"', _json.loads(req.data)["instruction"]).group(1)
        s = make_server(fresh_db, None, "hermes")
        stale = asyncio.run(s.call_tool("comms_reply_discord", {"text": "late call from an old turn", "turn_key": "wrong"}))
        assert '"no_turn"' in (stale[0] if isinstance(stale, tuple) else stale)[0].text
        asyncio.run(s.call_tool("comms_reply_discord", {"text": "Two options: A single service, B webhook + worker. I recommend B.", "turn_key": key}))
        asyncio.run(s.call_tool("sys_turn_done", {"summary": "answered the Boss", "turn_key": key}))

        class R(io.BytesIO):
            status = 202

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R(b"{}")

    host = Host("hermes", fresh_db, runner=webhook_runner(fresh_db, "s3cret", opener=opener, sleep=lambda s: None))
    out = host.run_once()
    assert out["ok"] and out["result"] == "answered the Boss"
    req = posted[0]
    body = req.data
    assert req.headers["X-hub-signature-256"] == "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert "options for the intake?" in _json.loads(body)["instruction"]
    assert "rule0.miss" not in events(fresh_db, "rule0%")


def test_hermes_without_done_signal_fails_the_turn(fresh_db):
    import io

    from blimp.mesh.hosts.hermes_host import webhook_runner

    b = Board(fresh_db)
    b.record_mandate("x", "t")
    t = b.create_task(title="Design", type="design", created_by="logan", mandate_id="M-0001", owner="hermes")
    b.send(msg((t["id"], t["context_id"]), frm="logan", to=("hermes",)))
    Dispatcher(fresh_db).route()

    class R(io.BytesIO):
        status = 202

        def __enter__(s):
            return s

        def __exit__(s, *a):
            return False
    run = webhook_runner(fresh_db, "s", opener=lambda r, timeout=15: R(b""), sleep=lambda s: None)
    host = Host("hermes", fresh_db, runner=lambda req: run({**req, "wall_clock_s": 0}))
    out = host.run_once()
    assert not out["ok"] and "no completion signal" in out["error"]
    assert "rule0.miss" in events(fresh_db, "rule0%")  # the Boss still hears something


def test_hermes_runner_refuses_without_secret(fresh_db):
    from blimp.mesh.hosts.hermes_host import webhook_runner
    with pytest.raises(ValueError):
        webhook_runner(fresh_db, "")
