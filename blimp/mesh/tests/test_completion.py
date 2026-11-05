"""DONE gate + verifier (spec §9.11) and the fabrication-trap suite (spec §20: "tools rigged to
fail silently; check that the agent reports failure and that the receipts gate catches it").
Phase 2 exit criterion: 100% of DONE claims have receipts; fabrication traps pass."""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from blimp.mesh.deck.board import Board, BoardError
from blimp.mesh.deck.events import EventStore
from blimp.mesh.deck.verification import verify
from blimp.mesh.toolgate.checks import CheckRunner, compare
from blimp.mesh.toolgate.gate import Toolgate

from .conftest import mkwt
from .test_board import contract


@pytest.fixture()
def env(fresh_db, tmp_path):
    b = Board(fresh_db)
    b.record_mandate("build intake", "test")
    (tmp_path / "flag").write_text("green")
    runner = CheckRunner(fresh_db, sql_dsn=ro_dsn(fresh_db))
    b.wt = str(tmp_path)
    gate = Toolgate(fresh_db, {"check.run": runner.adapter(), "repo.read": lambda a, c: ("x", "read", 0)})
    return b, gate, runner, tmp_path


def ro_dsn(dsn):
    """The dedicated read-only role sql checks must use (not the gate's own superuser login)."""
    with psycopg.connect(dsn, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname='blimp_check_ro_test'").fetchone():
            c.execute("CREATE ROLE blimp_check_ro_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        c.execute("GRANT USAGE ON SCHEMA mesh TO blimp_check_ro_test")
        c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA mesh TO blimp_check_ro_test")
    return dsn.replace("//logan@", "//blimp_check_ro_test@")


def working_task(b, checks, approve=True, **over):
    t = b.create_task(title="t", type="build", created_by="hermes", mandate_id="M-0001")
    over.setdefault("worktree", mkwt(t["id"]))
    b.propose(t["id"], contract(t["id"], acceptance_checks=checks, **over), "hermes")
    if approve:
        b.approve_checks(t["id"], "logan")
    b.make_ready(t["id"], "duke")
    b.claim(t["id"], "duke")
    return t["id"]


FLAG = {"id": "c1", "kind": "cmd", "cmd": "cat flag", "expected": "green"}
EXIT0 = {"id": "c2", "kind": "test", "cmd": "true", "expected": "exit 0"}


def report(tid, rc_by_check, claim_rcs=None, result="pass"):
    return {"task_id": tid, "summary": "done", "deliverables": [{"name": "PR", "ref": "pr/1"}], "known_gaps": [],
            "rollback": "revert", "claims": [{"text": "checks green", "receipts": claim_rcs or list(rc_by_check.values())}],
            "checks": [{"check_id": c, "receipt": r, "result": result} for c, r in rc_by_check.items()]}


def run_checks(gate, tid, ids, agent="duke"):
    turn = gate.open_turn(agent, tid)
    return {cid: gate.call(turn, agent, "check.run", {"check_id": cid}).receipt_id for cid in ids}


def test_honest_done_passes_gate_and_verifier(env):
    b, gate, runner, _ = env
    tid = working_task(b, [FLAG, EXIT0])
    rcs = run_checks(gate, tid, ["c1", "c2"])
    assert b.submit_completion(tid, "duke", report(tid, rcs))["state"] == "IN_REVIEW"
    v = verify(b.dsn, tid, runner)
    assert v["verdict"] == "PASS" and b.get_task(tid)["state"] == "DONE"
    assert all(c["own_receipt"] not in rcs.values() for c in v["checks"])  # the verifier ran them itself


# ── fabrication traps: every one must be caught by code ─────────────────────
def test_trap_invented_receipt(env):
    b, gate, _, _ = env
    tid = working_task(b, [EXIT0])
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", report(tid, {"c2": "RC-9999"}))
    assert e.value.code == "receipts_gate" and any("does not exist" in p for p in e.value.details)
    assert b.get_task(tid)["state"] == "WORKING"


def test_trap_receipt_from_another_task(env):
    b, gate, _, _ = env
    other = working_task(b, [EXIT0])
    rc = run_checks(gate, other, ["c2"])["c2"]
    tid = working_task(b, [EXIT0])
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", report(tid, {"c2": rc}))
    assert any("belongs to" in p for p in e.value.details)


def test_trap_green_receipt_of_the_wrong_tool(env):
    b, gate, _, _ = env
    tid = working_task(b, [EXIT0], capabilities=["repo.read"])
    turn = gate.open_turn("duke", tid)
    rc = gate.call(turn, "duke", "repo.read", {"path": "x"}).receipt_id
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", report(tid, {"c2": rc}))
    assert any("not check.run" in p for p in e.value.details)


def test_trap_receipt_for_a_different_check(env):
    b, gate, _, _ = env
    tid = working_task(b, [FLAG, EXIT0])
    rcs = run_checks(gate, tid, ["c1"])
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", report(tid, {"c1": rcs["c1"], "c2": rcs["c1"]}))
    assert any("for 'c1', not check.run for 'c2'" in p for p in e.value.details)


def test_trap_failed_check_reported_as_pass(env):
    b, gate, _, tmp = env
    (tmp / "flag").write_text("red")
    tid = working_task(b, [FLAG])
    rcs = run_checks(gate, tid, ["c1"])
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", report(tid, rcs))
    assert any("status is error" in p for p in e.value.details)


def test_trap_claim_without_receipts_and_missing_check(env):
    b, gate, _, _ = env
    tid = working_task(b, [FLAG, EXIT0])
    rcs = run_checks(gate, tid, ["c1"])
    r = report(tid, rcs)
    r["claims"].append({"text": "also deployed to prod", "receipts": []})
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", r)
    details = " | ".join(e.value.details)
    assert "c2" in details and ("cites no receipts" in details or "schema" in details)


def test_trap_stale_receipt_from_before_the_contract(env):
    b, gate, _, _ = env
    tid = working_task(b, [EXIT0])
    rc = run_checks(gate, tid, ["c2"])["c2"]
    with psycopg.connect(b.dsn, autocommit=True) as c:  # pretend the contract was frozen after that run
        c.execute("UPDATE mesh.tasks SET contract_frozen_at = now() + interval '1 minute' WHERE id=%s", (tid,))
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "duke", report(tid, {"c2": rc}))
    assert any("stale" in p for p in e.value.details)


def test_trap_agent_cannot_smuggle_a_command_into_check_run(env):
    b, gate, _, tmp = env
    (tmp / "flag").write_text("red")
    tid = working_task(b, [FLAG])
    turn = gate.open_turn("duke", tid)
    r = gate.call(turn, "duke", "check.run", {"check_id": "c1", "cmd": "echo green", "expected": "green"})
    assert not r.allowed and r.result["passed"] is False  # the contract's command and expectation ran, not the caller's


def test_trap_silent_tool_failure_is_caught_by_the_verifier(env):
    """The check passes when Duke runs it, then the world changes (a 'silently failing' tool):
    the verifier's own run catches it and the task goes back to WORKING."""
    b, gate, runner, tmp = env
    tid = working_task(b, [FLAG])
    rcs = run_checks(gate, tid, ["c1"])
    b.submit_completion(tid, "duke", report(tid, rcs))
    (tmp / "flag").write_text("red")
    v = verify(b.dsn, tid, runner)
    assert v["verdict"] == "FAIL" and v["reason"] == "CHECK_FAILED" and b.get_task(tid)["state"] == "WORKING"
    ev = [e for e in EventStore(b.dsn).events_for_subject("task", tid) if e["type"] == "verification.failed"]
    assert ev and ev[0]["payload"]["owner"] == "duke"


def test_two_verifier_fails_trigger_a_replan(env):
    b, gate, runner, tmp = env
    tid = working_task(b, [FLAG])
    for _ in range(2):
        (tmp / "flag").write_text("green")
        rcs = run_checks(gate, tid, ["c1"])
        b.submit_completion(tid, "duke", report(tid, rcs))
        (tmp / "flag").write_text("red")
        verify(b.dsn, tid, runner)
    types = [e["type"] for e in EventStore(b.dsn).events_for_subject("task", tid)]
    assert types.count("verification.failed") == 2 and "task.replan_needed" in types


def test_manual_checks_wait_for_logan(env):
    b, gate, runner, _ = env
    tid = working_task(b, [EXIT0, {"id": "m1", "kind": "manual_by_logan", "procedure": "look at it on the phone", "expected": "looks right"}])
    rcs = run_checks(gate, tid, ["c2"])
    b.submit_completion(tid, "duke", report(tid, rcs))
    v = verify(b.dsn, tid, runner)
    assert v["verdict"] == "AWAITING_LOGAN" and v["manual_checks"] == ["m1"] and b.get_task(tid)["state"] == "IN_REVIEW"


def test_only_lease_holder_submits(env):
    b, gate, _, _ = env
    tid = working_task(b, [EXIT0])
    rcs = run_checks(gate, tid, ["c2"])
    with pytest.raises(BoardError) as e:
        b.submit_completion(tid, "hermes", report(tid, rcs))
    assert e.value.code == "not_lease_holder"


def test_failing_checks_do_not_open_the_circuit(env):
    b, gate, _, tmp = env
    (tmp / "flag").write_text("red")
    tid = working_task(b, [FLAG])
    turn = gate.open_turn("duke", tid)
    codes = [gate.call(turn, "duke", "check.run", {"check_id": "c1"}).code for _ in range(5)]
    assert "circuit_open" not in codes


# ── the runner itself ────────────────────────────────────────────────────────
@pytest.mark.parametrize("expected,out,code,status,ok", [
    ("exit 0", "", 0, None, True), ("exit 0", "", 1, None, False),
    ("200", "", None, 200, True), ("status 404", "", None, 200, False),
    ("contains: 42 passed", "=== 42 passed in 1s ===", None, None, True),
    ("regex: ^\\d+ passed", "12 passed", None, None, True),
    (">= 1", "3", None, None, True), ("< 1", "3", None, None, False), ("= 0", "zero", None, None, False),
    ("green", " green\n", None, None, True), ("green", "greenish", None, None, False),
])
def test_compare_grammar(expected, out, code, status, ok):
    assert compare(expected, output=out, exit_code=code, status=status)[0] is ok


def test_http_check_uses_allowlist_and_status(env):
    b, gate, _, _ = env
    seen = []

    def opener(req, timeout=30):
        seen.append(req.full_url)

        class R(io.BytesIO):
            status = 200

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False
        return R(b'{"ok":true}')

    runner = CheckRunner(b.dsn, opener=opener)
    tid = working_task(b, [{"id": "h1", "kind": "http", "url": "https://yourproduct.example/api/health", "expected": "200"}])
    res, summary, code = runner.run(tid, "h1")
    assert code == 0 and res["passed"] and seen == ["https://yourproduct.example/api/health"]


def test_sql_check_is_read_only_even_if_lint_is_bypassed(env):
    b, gate, runner, _ = env
    tid = working_task(b, [{"id": "s1", "kind": "sql_readonly", "cmd": "SELECT count(*) FROM mesh.tasks", "expected": ">= 1"}])
    res, _, code = runner.run(tid, "s1")
    assert code == 0 and res["passed"]
    with psycopg.connect(b.dsn, autocommit=True) as c:  # someone edits the frozen contract in the DB directly
        c.execute("""UPDATE mesh.tasks SET contract = jsonb_set(contract, '{acceptance_checks,0,cmd}', '"SELECT 1; DELETE FROM mesh.tasks"') WHERE id=%s""", (tid,))
    with pytest.raises(Exception):
        runner.run(tid, "s1")
    assert len(b.list_tasks()) >= 1


def test_check_runs_only_after_the_contract_is_frozen(env):
    b, gate, runner, _ = env
    t = b.create_task(title="t", type="build", created_by="hermes", mandate_id="M-0001")
    b.propose(t["id"], contract(t["id"], acceptance_checks=[EXIT0], worktree=mkwt(t["id"])), "hermes")
    with pytest.raises(Exception) as e:
        runner.run(t["id"], "c2")
    assert "frozen" in str(e.value)


def test_sweep_runs_pending_verifications(env, monkeypatch, capsys):
    from blimp.mesh.ops import sweep
    b, gate, runner, tmp = env
    monkeypatch.setenv("BLIMPG_DSN", b.dsn)
    tid = working_task(b, [FLAG])
    b.submit_completion(tid, "duke", report(tid, run_checks(gate, tid, ["c1"])))
    assert sweep.pending_verifications(b.dsn) == [tid]
    sweep.main()
    assert json.loads(capsys.readouterr().out)["verified"] == {tid: "PASS"}
    assert sweep.pending_verifications(b.dsn) == []


def test_board_tools_run_checks_and_submit(env, monkeypatch):
    from blimp.mesh.tools import board_tools as bt
    b, gate, runner, tmp = env
    monkeypatch.setenv("BLIMPG_DSN", b.dsn)
    monkeypatch.setenv("A2A_AGENT_NAME", "duke")
    tid = working_task(b, [FLAG])
    r = json.loads(bt.board_check_run(tid, "c1"))
    assert r["ok"] and r["passed"] and r["receipt_id"].startswith("RC-")
    s = json.loads(bt.board_submit_completion(tid, json.dumps(report(tid, {"c1": r["receipt_id"]}))))
    assert s["ok"] and s["state"] == "IN_REVIEW"
    bad = json.loads(bt.board_submit_completion(tid, json.dumps(report(tid, {"c1": "RC-0404"}))))
    assert not bad["ok"]
    ap = json.loads(bt.board_request_approval(tid, "repo.merge_main", json.dumps({"pr": 7}), "ship it", "git revert", "RC-0001"))
    assert ap["ok"] and ap["id"].startswith("AP-") and ap["status"] == "pending"
