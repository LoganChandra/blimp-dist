"""Toolgate MCP server (spec §13.1, §10.4): the host's only tool surface; identity from env."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
import pytest

from blimp.mesh.deck.board import Board
from blimp.mesh.toolgate.gate import Toolgate

from .test_board import LEDGER, contract

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def turn(fresh_db, tmp_path, monkeypatch):
    b = Board(fresh_db)
    b.record_mandate("x", "t")
    t = b.create_task(title="t", type="build", created_by="hermes", mandate_id="M-0001")
    from .conftest import mkwt
    wt = mkwt(t["id"])
    (Path(wt) / "src.py").write_text("print('hello')\nSECRET_TODO = 1\n")
    b.propose(t["id"], contract(t["id"], worktree=wt, capabilities=["repo.read", "repo.worktree"]), "hermes")
    b.make_ready(t["id"], "duke")
    b.claim(t["id"], "duke")
    tu = Toolgate(fresh_db).open_turn("duke", t["id"])
    monkeypatch.setenv("BLIMPG_DSN", fresh_db)
    return b, t["id"], tu, tmp_path


def call(server, name, args=None, **kw):
    out = asyncio.run(server.call_tool(name, {**(args or {}), **kw}))
    content = out[0] if isinstance(out, tuple) else out
    return json.loads(content[0].text)


def test_tools_listed_and_described(turn):
    from blimp.mesh.toolgate.mcp_server import TOOLS, make_server
    b, tid, tu, wt = turn
    s = make_server(b.dsn, tu, "duke")
    names = {t.name for t in asyncio.run(s.list_tools())}
    assert names == set(TOOLS)
    descs = {t.name: t.description for t in asyncio.run(s.list_tools())}
    assert all(20 < len(d) <= 600 for d in descs.values())


def test_calls_go_through_the_gate_with_receipts(turn):
    from blimp.mesh.toolgate.mcp_server import make_server
    b, tid, tu, wt = turn
    s = make_server(b.dsn, tu, "duke")
    r = call(s, "repo_read", {"path": "src.py"})
    assert r["ok"] and "hello" in r["result"]["content"] and r["receipt_id"].startswith("RC-")
    esc = call(s, "repo_read", {"path": "../../../etc/passwd"})
    assert not esc["ok"] and "outside the task worktree" in esc["reason"]
    g = call(s, "repo_grep", {"pattern": "SECRET_TODO"})
    assert g["ok"] and "src.py:2" in g["result"]["matches"]
    assert call(s, "board_ledger_update", {"objective_restated": "intake", "plan": [{"step": 1, "what": "x", "status": "doing"}],
                                            "next_step": "y"})["result"]["version"] == 1
    assert call(s, "sys_whoami")["result"]["agent"] == "duke"


def test_repo_run_refused_without_a_sandbox(turn, monkeypatch):
    from blimp.mesh.toolgate.mcp_server import make_server
    monkeypatch.delenv("BLIMP_SANDBOX", raising=False)
    b, tid, tu, wt = turn
    r = call(make_server(b.dsn, tu, "duke"), "repo_run", {"cmd": "rm -rf ~"})
    assert not r["ok"] and "sandbox" in r["reason"]


def test_reply_discord_is_queued_and_rule0_can_see_it(turn):
    from blimp.mesh.toolgate.mcp_server import make_server
    b, tid, tu, wt = turn
    r = call(make_server(b.dsn, tu, "duke"), "comms_reply_discord", {"text": "Intake is at step 2 of 3. Needs you: nothing."})
    assert r["ok"]
    with psycopg.connect(b.dsn) as c:
        assert c.execute("SELECT channel, persona FROM mesh.outbox WHERE op='post'").fetchone() == ("dm:duke", "duke")
        assert c.execute("SELECT count(*) FROM mesh.receipts WHERE tool='comms.reply_discord' AND status='ok'").fetchone()[0] == 1


def test_approval_request_goes_through_the_gate_and_taint(turn):
    from blimp.mesh.toolgate.mcp_server import make_server
    b, tid, tu, wt = turn
    s = make_server(b.dsn, tu, "duke")
    r = call(s, "sys_request_approval", {"tool": "repo.merge_main", "args": {"pr": 9}, "why": "ship", "rollback": "revert"})
    assert r["ok"] and r["result"]["id"].startswith("AP-"), r
    with psycopg.connect(b.dsn, autocommit=True) as c:
        c.execute("UPDATE mesh.turn_state SET tainted=true WHERE turn_id=%s", (tu,))
    again = call(s, "sys_request_approval", {"tool": "repo.merge_main", "args": {"pr": 9}, "why": "ship", "rollback": "revert"})
    assert not again["ok"] and again["code"] == "tainted_r2"


def test_refused_input_is_a_result_and_never_trips_the_circuit(turn):
    from blimp.mesh.toolgate.mcp_server import make_server
    b, tid, tu, wt = turn
    s = make_server(b.dsn, tu, "duke")
    for _ in range(4):
        r = call(s, "board_ledger_update", {"objective_restated": "x", "plan": [{"step": 1, "what": "a", "status": "finished"}], "next_step": "y"})
        assert not r["ok"] and "plan[0]" in r["reason"]
    for _ in range(3):
        assert not call(s, "repo_read", {"path": "../../etc"})["ok"]
    good = call(s, "board_ledger_update", {"objective_restated": "x", "plan": [{"step": 1, "what": "a", "status": "doing"}], "next_step": "y"})
    assert good["ok"], good
    assert call(s, "repo_read", {"path": "src.py"})["ok"]


def test_stdio_server_speaks_mcp(turn):
    """The real process a host will start: identity only from env, over stdio."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    b, tid, tu, wt = turn

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "blimp.mesh.toolgate.mcp_server"], cwd=str(ROOT),
                                       env={**os.environ, "BLIMPG_DSN": b.dsn, "BLIMP_TURN_ID": tu, "BLIMP_AGENT": "duke"})
        async with stdio_client(params) as (r, w), ClientSession(r, w) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            res = await sess.call_tool("sys_whoami", {})
            return {t.name for t in tools.tools}, json.loads(res.content[0].text)

    names, who = asyncio.run(go())
    assert "repo_read" in names and who["result"]["turn_id"] == tu and who["result"]["agent"] == "duke"
