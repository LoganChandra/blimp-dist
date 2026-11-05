"""Brain-MCP board tools (spec §23 phase 1): identity from the environment, errors as results."""
from __future__ import annotations

import json

import pytest

from blimp.mesh.tools import board_tools as bt

from .test_board import LEDGER, contract


def call(fn, agent, monkeypatch, *a, **kw):
    monkeypatch.setenv("A2A_AGENT_NAME", agent)
    return json.loads(fn(*a, **kw))


@pytest.fixture()
def env(fresh_db, monkeypatch):
    monkeypatch.setenv("BLIMPG_DSN", fresh_db)
    r = call(bt.board_mandate, "nanook", monkeypatch, "build the intake please", "discord:#nanook:1")
    assert r["ok"] and r["id"] == "M-0001"
    return monkeypatch


def test_ulid_shape_and_order():
    a, b = bt.ulid(), bt.ulid()
    assert len(a) == 26 and set(a) <= set(bt._ULID_ALPHABET) and a != b


def test_nanook_is_frank(env):
    t = call(bt.board_task_new, "nanook", env, "Weekly report", "report", mandate_id="M-0001")
    assert t["ok"] and t["owner"] == "frank"


def test_full_flow_through_tools(env):
    t = call(bt.board_task_new, "hermes", env, "Webhook intake", "build", mandate_id="M-0001")
    tid = t["id"]
    p = call(bt.board_task_propose, "hermes", env, tid, json.dumps(contract(tid)))
    assert p["ok"] and p["lint_ok"], p
    v = call(bt.board_task, "duke", env, tid)["task"]["version"]
    assert call(bt.board_task_accept, "hermes", env, tid, v)["error"] == "not_owner"
    assert call(bt.board_task_accept, "duke", env, tid, v - 1)["error"] == "stale_version"
    assert call(bt.board_task_accept, "duke", env, tid, v)["state"] == "READY"
    assert call(bt.board_task_claim, "duke", env, tid)["state"] == "WORKING"
    assert call(bt.board_ledger, "duke", env, tid, json.dumps(LEDGER))["version"] == 1
    s = call(bt.board_send, "duke", env, "hermes", "QUERY", "QUERY: retention period for raw payloads?", tid,
             "retention question", expects="answer", reply_within_minutes=120)
    assert s["ok"], s
    inbox = call(bt.board_inbox, "hermes", env)
    assert [m["text"] for m in inbox["messages"]] == ["QUERY: retention period for raw payloads?"]
    assert call(bt.board_inbox, "hermes", env)["messages"] == []  # read receipts set by the first read
    task = call(bt.board_task, "frank", env, tid)
    assert task["task"]["state"] == "WORKING" and task["ledger"]["version"] == 1


def test_identity_cannot_be_spoofed(env):
    """The board knows who you are from the process environment only; there is no 'from' argument."""
    import inspect
    for fn in bt.ALL:
        params = inspect.signature(fn).parameters
        assert not ({"sender", "from_agent", "actor", "agent", "as_agent"} & set(params)), fn.__name__
    r = call(bt.board_task_new, "other-agent", env, "hijack", "maintenance")
    assert not r["ok"] and r["error"] == "unknown_principal"
    r = call(bt.board_task_new, "", env, "anon", "maintenance")
    assert not r["ok"]


def test_errors_are_results_not_exceptions(env):
    for r in [
        call(bt.board_task_propose, "hermes", env, "T-9999", "{not json"),
        call(bt.board_task, "hermes", env, "T-9999"),
        call(bt.board_task_move, "duke", env, "T-9999", "blocked", "x"),
        call(bt.board_send, "duke", env, "hermes", "INFORM", "ok thanks", "T-9999", "x"),
        call(bt.board_ledger, "duke", env, "T-9999", "[1,2]"),
    ]:
        assert r["ok"] is False and r["error"], r


def test_new_work_request_via_tools(env):
    t = call(bt.board_task_new, "hermes", env, "Design intake", "design", mandate_id="M-0001")
    r = call(bt.board_send, "hermes", env, "duke", "REQUEST", "REQUEST: build Telegram intake per AR-0311", "new",
             "Telegram intake build", expects="accept", reply_within_minutes=240, new_task_title="Telegram intake",
             new_task_type="build", new_task_mandate_id="M-0001")
    assert r["ok"], r
    new = call(bt.board_task, "duke", env, r["task_id"])
    assert new["task"]["state"] == "PROPOSED" and new["task"]["owner"] == "duke"


def test_mandate_records_who_typed_it(env):
    import psycopg
    with psycopg.connect(bt._board().dsn) as c:
        tags = c.execute("SELECT tags FROM mesh.mandates WHERE id='M-0001'").fetchone()[0]
    assert "recorded_by:frank" in tags
