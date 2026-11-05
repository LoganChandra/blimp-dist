"""Verification workflow, deterministic core (spec §9.11 steps 3-5, §12.7.1).

When a task enters IN_REVIEW, the verifier RE-RUNS every runnable acceptance check itself,
through Toolgate as `svc-verifier` (read-only, its own receipts), and never trusts the
reported output. Verdict:
  PASS          every runnable check passed on the verifier's own run, and there are no
                manual checks -> the task becomes DONE (dependents are unblocked)
  FAIL          any check failed or could not run ("unverifiable means FAIL")
                -> back to WORKING with findings; two FAILs trigger a replan
  AWAITING_LOGAN  runnable checks passed but manual_by_logan/review checks remain
                -> stays IN_REVIEW; the manual checks go to the Boss's decision window

The fresh-context LLM inspection of diffs for scope/secrets/PII (spec 12.7.1 second bullet)
runs in the verifier service agent from phase 3; this module is the part that needs no model.
"""
from __future__ import annotations

from typing import Any

import psycopg

from ..toolgate.checks import CheckRunner
from ..toolgate.gate import Toolgate
from .board import Board

MANUAL = {"manual_by_logan", "review"}


def verify(dsn: str, task_id: str, runner: CheckRunner | None = None) -> dict[str, Any]:
    board = Board(dsn)
    task = board.get_task(task_id)
    if task["state"] != "IN_REVIEW":
        return {"task_id": task_id, "verdict": "SKIPPED", "reason": f"task is {task['state']}"}
    runner = runner or CheckRunner(dsn)
    gate = Toolgate(dsn, {"check.run": runner.adapter()})
    turn = gate.open_turn("svc-verifier", task_id)
    checks, findings, manual = [], [], []
    for chk in task["contract"].get("acceptance_checks") or []:
        if chk.get("kind") in MANUAL:
            manual.append(chk["id"])
            continue
        r = gate.call(turn, "svc-verifier", "check.run", {"check_id": chk["id"]})
        result = "pass" if r.allowed else ("unverifiable" if r.code in ("not_wired", "denied") or r.result is None else "fail")
        checks.append({"check_id": chk["id"], "own_receipt": r.receipt_id, "result": result, "note": r.reason[:300]})
        if result != "pass":
            findings.append({"severity": "blocker", "text": f"{chk['id']}: {r.reason[:300]}", "receipt": r.receipt_id})
    report = {"task_id": task_id, "checks": checks, "findings": findings, "verifier_model": "deterministic-check-runner"}
    if findings:
        reasons = {c["result"] for c in checks if c["result"] != "pass"}
        report.update(verdict="FAIL", reason="UNVERIFIABLE" if reasons == {"unverifiable"} else "CHECK_FAILED")
        board.verification_failed(task_id, "svc-verifier", report)
    elif manual:
        report.update(verdict="AWAITING_LOGAN", reason="OTHER", manual_checks=manual)
        with psycopg.connect(dsn) as c:
            from .events import append_event

            with c.cursor() as cur:
                append_event(cur, "svc-verifier", "verification.awaiting_logan", report, "task", task_id, correlation_id=task_id)
    else:
        report.update(verdict="PASS", reason="ALL_CHECKS_PASSED")
        board.mark_done(task_id, "svc-verifier", report)
    return report
