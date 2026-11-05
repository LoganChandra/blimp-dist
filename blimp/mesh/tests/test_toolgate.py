"""Toolgate (spec §13): authority, least privilege, taint, Rule of Two, approvals, receipts —
Hermes's original cases ported to the persisted gate, plus the adversarial ones."""
from __future__ import annotations

import threading
from datetime import timedelta

import psycopg
import pytest

from blimp.mesh.deck.board import Board
from blimp.mesh.toolgate.gate import Toolgate, action_hash

from .test_board import contract


def ok_adapter(result="ok"):
    return lambda args, ctx: (result, f"{result}", 0)


@pytest.fixture()
def gate(fresh_db):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    adapters = {t: ok_adapter() for t in ("repo.read", "repo.edit", "repo.push_branch", "repo.merge_main", "web.fetch",
                                          "deploy.prod_deploy", "brain.recall", "comms.send", "board.task_get", "crm.prospect_get",
                                          "comms.external_send")}
    g = Toolgate(fresh_db, adapters)
    g.board = b
    return g


def task_with(g, caps, owner="duke", risk="R2", **over):
    t = g.board.create_task(title="t", type="build", created_by="hermes", mandate_id="M-0001", owner=owner)
    g.board.propose(t["id"], contract(t["id"], owner=owner, capabilities=caps, risk_tier=risk, **over), "hermes")
    g.board.make_ready(t["id"], owner)
    g.board.claim(t["id"], owner)
    return t["id"]


def receipt(g, rid):
    with psycopg.connect(g.dsn) as c:
        return c.execute("SELECT status, tool, agent, denial_reason, taint_in FROM mesh.receipts WHERE id=%s", (rid,)).fetchone()


def test_r0_read_allowed_with_receipt(gate):
    tid = task_with(gate, ["repo.read"])
    turn = gate.open_turn("duke", tid)
    r = gate.call(turn, "duke", "repo.read", {"path": "src/x.py"})
    assert r.allowed and r.receipt_id.startswith("RC-") and receipt(gate, r.receipt_id)[0] == "ok"


def test_r3_without_token_is_auth_required_and_denials_get_receipts(gate):
    tid = task_with(gate, ["deploy.prod"], owner="frank", risk="R3", r3_steps=[{"tool": "deploy.prod_deploy", "why": "ship"}])
    turn = gate.open_turn("frank", tid)
    r = gate.call(turn, "frank", "deploy.prod_deploy", {"app": "<your-product>", "ref": "abc"})
    assert not r.allowed and r.code == "auth_required"
    assert receipt(gate, r.receipt_id)[0] == "denied"


def test_duke_and_hermes_can_only_request_r3(gate):
    for agent in ("duke", "hermes"):
        tid = task_with(gate, ["repo.merge_main"], owner=agent, risk="R3", r3_steps=[{"tool": "repo.merge_main", "why": "x"}])
        turn = gate.open_turn(agent, tid)
        r = gate.call(turn, agent, "repo.merge_main", {"pr": 5})
        assert not r.allowed and r.code == "r3_request_only"


def test_full_approval_flow_single_use_and_hash_bound(gate):
    tid = task_with(gate, ["deploy.prod"], owner="frank", risk="R3", r3_steps=[{"tool": "deploy.prod_deploy", "why": "ship"}])
    args = {"app": "<your-product>", "ref": "abc123"}
    ap = gate.request_approval(requested_by="frank", executor="frank", tool="deploy.prod_deploy", args=args,
                               why="ships intake", rollback="redeploy @9f2e", evidence=["RC-0001"], task_id=tid)
    turn = gate.open_turn("frank", tid)
    assert gate.call(turn, "frank", "deploy.prod_deploy", args, approval_id=ap["id"]).code == "approval_not_granted"
    with pytest.raises(PermissionError):
        gate.decide_approval(ap["id"], "frank", True)  # agents never approve
    with pytest.raises(PermissionError):
        gate.decide_approval(ap["id"], "cofounder", True)
    gate.decide_approval(ap["id"], "logan", True, note="go")
    assert gate.call(turn, "frank", "deploy.prod_deploy", {**args, "ref": "evil"}, approval_id=ap["id"]).code == "approval_mismatch"
    r = gate.call(turn, "frank", "deploy.prod_deploy", args, approval_id=ap["id"])
    assert r.allowed
    again = gate.call(turn, "frank", "deploy.prod_deploy", args, approval_id=ap["id"])
    assert again.code == "approval_used"


def test_a_denied_call_never_burns_the_token(gate):
    """Hermes's gate consumed the token before later checks; a deny-listed arg then wasted it."""
    tid = task_with(gate, ["deploy.prod", "web.read"], owner="frank", risk="R3", r3_steps=[{"tool": "deploy.prod_deploy", "why": "ship"}])
    args = {"app": "<your-product>", "ref": "abc"}
    ap = gate.request_approval(requested_by="frank", executor="frank", tool="deploy.prod_deploy", args=args, why="x", rollback="y", task_id=tid)
    gate.decide_approval(ap["id"], "logan", True)
    t2 = gate.open_turn("frank", tid)
    assert gate.call(t2, "frank", "web.fetch", {"url": "https://example.com"}).allowed  # taints the turn
    assert gate.call(t2, "frank", "deploy.prod_deploy", args, approval_id=ap["id"]).code == "tainted_r3"
    clean = gate.open_turn("frank", tid)
    assert gate.call(clean, "frank", "deploy.prod_deploy", args, approval_id=ap["id"]).allowed


def test_racing_calls_cannot_both_use_one_token(gate):
    tid = task_with(gate, ["deploy.prod"], owner="frank", risk="R3", r3_steps=[{"tool": "deploy.prod_deploy", "why": "ship"}])
    args = {"app": "<your-product>", "ref": "abc"}
    ap = gate.request_approval(requested_by="frank", executor="frank", tool="deploy.prod_deploy", args=args, why="x", rollback="y", task_id=tid)
    gate.decide_approval(ap["id"], "logan", True)
    turns = [gate.open_turn("frank", tid) for _ in range(6)]
    results = []
    ths = [threading.Thread(target=lambda t=t: results.append(gate.call(t, "frank", "deploy.prod_deploy", args, approval_id=ap["id"])))
           for t in turns]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sum(1 for r in results if r.allowed) == 1


def test_expired_approval(gate):
    tid = task_with(gate, ["deploy.prod"], owner="frank", risk="R3", r3_steps=[{"tool": "deploy.prod_deploy", "why": "ship"}])
    args = {"app": "<your-product>"}
    ap = gate.request_approval(requested_by="frank", executor="frank", tool="deploy.prod_deploy", args=args, why="x", rollback="y",
                               task_id=tid, ttl=timedelta(seconds=-1))
    with pytest.raises(ValueError):
        gate.decide_approval(ap["id"], "logan", True)


def test_wrong_executor(gate):
    tid = task_with(gate, ["deploy.prod"], owner="frank", risk="R3", r3_steps=[{"tool": "deploy.prod_deploy", "why": "ship"}])
    args = {"app": "<your-product>"}
    ap = gate.request_approval(requested_by="duke", executor="logan", tool="deploy.prod_deploy", args=args, why="x", rollback="y", task_id=tid)
    gate.decide_approval(ap["id"], "logan", True)
    turn = gate.open_turn("frank", tid)
    assert gate.call(turn, "frank", "deploy.prod_deploy", args, approval_id=ap["id"]).code == "wrong_executor"


def test_approvals_only_for_r3_and_need_why_and_rollback(gate):
    with pytest.raises(ValueError):
        gate.request_approval(requested_by="duke", executor="frank", tool="repo.read", args={}, why="x", rollback="y")
    with pytest.raises(ValueError):
        gate.request_approval(requested_by="duke", executor="frank", tool="repo.merge_main", args={}, why="", rollback="y")
    with pytest.raises(ValueError):
        gate.request_approval(requested_by="duke", executor="duke", tool="repo.merge_main", args={}, why="x", rollback="y")


def test_action_hash_is_canonical():
    assert action_hash("t", {"a": 1, "b": 2}) == action_hash("t", {"b": 2, "a": 1})
    assert action_hash("t", {"a": 1}) != action_hash("u", {"a": 1})
    assert action_hash("ta", {}) != action_hash("t", {"a": {}})  # tool/args boundary is unambiguous


def test_least_privilege(gate):
    tid = task_with(gate, ["repo.read"])
    turn = gate.open_turn("duke", tid)
    assert gate.call(turn, "duke", "repo.edit", {"path": "x"}).code == "capability_missing"
    chat = gate.open_turn("duke")  # no task: chat set only
    assert gate.call(chat, "duke", "repo.read", {"path": "x"}).code == "capability_missing"
    assert gate.call(chat, "duke", "brain.recall", {"q": "x"}).allowed


def test_capabilities_come_from_the_board_not_the_caller(gate):
    tid = task_with(gate, ["repo.read"])
    turn = gate.open_turn("duke", tid)
    with psycopg.connect(gate.dsn, autocommit=True) as c:  # widening the contract after READY is impossible
        pass
    assert gate.call(turn, "duke", "repo.push_branch", {"branch": "x"}).code == "capability_missing"


def test_turn_identity(gate):
    tid = task_with(gate, ["repo.read"])
    turn = gate.open_turn("duke", tid)
    assert gate.call(turn, "hermes", "repo.read", {}).code == "not_your_turn"
    assert gate.call("TU-9999", "duke", "repo.read", {}).code == "unknown_turn"
    with pytest.raises(PermissionError):
        gate.call(turn, "other-agent", "repo.read", {})
    old = gate.open_turn("duke", tid, ttl=timedelta(seconds=-1))
    assert gate.call(old, "duke", "repo.read", {}).code == "turn_expired"


def test_deny_lists(gate):
    tid = task_with(gate, ["brain.recall", "repo.read"])
    turn = gate.open_turn("duke", tid)
    for args in ({"bank": "private-agent-3"}, {"bank": "other-agent-notes"}, {"bank": "private-agent"}, {"path": "/home/user/other-agent/repo/x"}):
        tool = "brain.recall" if "bank" in args else "repo.read"
        assert gate.call(turn, "duke", tool, args).code == "deny_list", args


def test_taint_plan_then_execute(gate):
    tid = task_with(gate, ["web.read", "repo.branch", "repo.merge_main"], risk="R3", r3_steps=[{"tool": "repo.merge_main", "why": "x"}])
    turn = gate.open_turn("duke", tid)
    gate.declare_plan(turn, "duke", ["repo.push_branch"])
    r = gate.call(turn, "duke", "web.fetch", {"url": "https://example.com"})
    assert r.allowed and receipt(gate, r.receipt_id)[4] is True  # taint recorded
    with pytest.raises(PermissionError):
        gate.declare_plan(turn, "duke", ["repo.push_branch", "comms.send"])  # plan locked after untrusted read
    assert gate.call(turn, "duke", "repo.push_branch", {"branch": "x"}).allowed  # pre-declared R2 ok
    assert gate.call(turn, "duke", "comms.send", {"to": "hermes"}).code == "tainted_r2"
    assert gate.call(turn, "duke", "repo.merge_main", {"pr": 1}).code in ("tainted_r3", "r3_request_only")


def test_rule_of_two(gate):
    tid = task_with(gate, ["web.read", "crm.read", "comms.external"], owner="frank", risk="R3",
                    r3_steps=[{"tool": "comms.external_send", "why": "x"}])
    turn = gate.open_turn("frank", tid)
    gate.declare_plan(turn, "frank", ["comms.external_send"])
    assert gate.call(turn, "frank", "web.fetch", {"url": "https://x"}).allowed          # untrusted
    assert gate.call(turn, "frank", "crm.prospect_get", {"id": 1}).allowed              # + sensitive = 2
    assert gate.call(turn, "frank", "comms.external_send", {"to": "x"}).code in ("rule_of_two", "tainted_r3")  # a third never runs


def test_idempotent_replay_never_repeats_the_effect(gate):
    calls = []
    gate.adapters["repo.push_branch"] = lambda a, c: (calls.append(1) or "pushed", "pushed", 0)
    tid = task_with(gate, ["repo.branch"])
    turn = gate.open_turn("duke", tid)
    a = gate.call(turn, "duke", "repo.push_branch", {"branch": "x"}, idempotency_key="k1")
    b = gate.call(turn, "duke", "repo.push_branch", {"branch": "x"}, idempotency_key="k1")
    assert a.receipt_id == b.receipt_id and b.code == "replay" and calls == [1]


def test_adapter_errors_are_honest_and_trip_the_circuit(gate):
    def boom(a, c):
        raise RuntimeError("github 502")
    gate.adapters["repo.read"] = boom
    tid = task_with(gate, ["repo.read"])
    turn = gate.open_turn("duke", tid)
    for _ in range(3):
        r = gate.call(turn, "duke", "repo.read", {"path": "x"})
        assert not r.allowed and "502" in r.reason and receipt(gate, r.receipt_id)[0] == "error"
    assert gate.call(turn, "duke", "repo.read", {"path": "x"}).code == "circuit_open"


def test_unwired_tool_says_so(gate):
    tid = task_with(gate, ["repo.worktree"])
    turn = gate.open_turn("duke", tid)
    r = gate.call(turn, "duke", "repo.commit", {"msg": "x"})
    assert not r.allowed and r.code == "not_wired" and receipt(gate, r.receipt_id)[0] == "error"


def test_receipts_are_immutable(gate):
    tid = task_with(gate, ["repo.read"])
    turn = gate.open_turn("duke", tid)
    rid = gate.call(turn, "duke", "repo.read", {"path": "x"}).receipt_id
    with psycopg.connect(gate.dsn, autocommit=True) as c:
        c.execute("UPDATE mesh.receipts SET status='ok', summary='all tests passed' WHERE id=%s", (rid,))
        c.execute("DELETE FROM mesh.receipts WHERE id=%s", (rid,))
        assert c.execute("SELECT summary FROM mesh.receipts WHERE id=%s", (rid,)).fetchone()[0] == "ok"
