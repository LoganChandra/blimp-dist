"""Blimp Crew v2 — Receipts gate (spec §9.11 step 2): the deterministic anti-fabrication gate.

"This is not an LLM judgment." A DONE passes only if:
  - the completion report validates against schemas/completion.v1.json;
  - every claim cites at least one receipt, and every cited receipt exists, belongs to this
    task, has status=ok, and ended after the contract was frozen (nothing reused from before
    the work, or from another task);
  - every runnable acceptance check has a receipt that is a `check.run` receipt FOR THAT
    CHECK (a green `repo.read` cannot stand in for a test run), status=ok (the check passed),
    and fresh by the same rule;
  - the report does not claim a check passed while its receipt says otherwise.
Manual checks (manual_by_logan, review) go to the Boss's decision window instead.
On failure the DONE is rejected with the exact list of problems. Zero LLM.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "completion.v1.json"
MANUAL = {"manual_by_logan", "review"}


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator

    return Draft202012Validator(json.loads(SCHEMA.read_text()))


class ReceiptsGate:
    def __init__(self, fetch_receipt: Callable[[str], dict[str, Any] | None]):
        """fetch_receipt(receipt_id) -> {task_id, tool, status, check_id, ended_at} | None."""
        self.fetch_receipt = fetch_receipt

    def check(self, task_id: str, report: dict[str, Any], acceptance_checks: list[dict[str, Any]],
              frozen_at: datetime | None) -> tuple[bool, list[str]]:
        problems = [f"schema {'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
                    for e in _validator().iter_errors(report)]
        if report.get("task_id") not in (None, task_id):
            problems.append(f"report is for {report.get('task_id')}, not {task_id}")
        cache: dict[str, dict[str, Any] | None] = {}

        def get(rid: str) -> dict[str, Any] | None:
            if rid not in cache:
                cache[rid] = self.fetch_receipt(rid)
            return cache[rid]

        def valid(rid: str, where: str) -> dict[str, Any] | None:
            r = get(rid)
            if r is None:
                problems.append(f"{where}: receipt {rid} does not exist")
                return None
            if r.get("task_id") != task_id:
                problems.append(f"{where}: receipt {rid} belongs to {r.get('task_id')}, not {task_id}")
                return None
            if r.get("status") != "ok":
                problems.append(f"{where}: receipt {rid} status is {r.get('status')}, not ok")
                return None
            if frozen_at and r.get("ended_at") and r["ended_at"] < frozen_at:
                problems.append(f"{where}: receipt {rid} predates the frozen contract (stale evidence)")
                return None
            return r

        for i, claim in enumerate(report.get("claims") or []):
            rcs = claim.get("receipts") or []
            if not rcs:
                problems.append(f"claim[{i}] cites no receipts: {str(claim.get('text', ''))[:60]!r}")
            for rid in rcs:
                valid(rid, f"claim[{i}]")

        reported = {c.get("check_id"): c for c in report.get("checks") or []}
        for chk in acceptance_checks:
            cid = chk.get("id")
            if chk.get("kind") in MANUAL:
                continue
            entry = reported.get(cid)
            if not entry or not entry.get("receipt"):
                problems.append(f"acceptance check {cid!r} has no receipt in the report")
                continue
            r = valid(entry["receipt"], f"check {cid!r}")
            if r is None:
                continue
            if r.get("tool") != "check.run" or r.get("check_id") != cid:
                problems.append(f"check {cid!r}: receipt {entry['receipt']} is a {r.get('tool')} receipt"
                                f"{' for ' + repr(r.get('check_id')) if r.get('check_id') else ''}, not check.run for {cid!r}")
            if entry.get("result") != "pass":
                problems.append(f"check {cid!r} is reported as {entry.get('result')!r}, not pass")
        return (not problems), problems
