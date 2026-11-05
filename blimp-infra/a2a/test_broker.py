"""
test_broker.py — broker lifecycle/enforcement tests. No live service, no live DB.

Covers (P0.6 + P1.5):
  - legal transitions stamp the right state + timestamp
  - illegal / terminal transitions get HTTP 409
  - unknown target state gets HTTP 400
  - completion schema gate (P1.5): conforming payload completes; non-conforming
    is rejected (422) and counts as a bounce; the 2nd reject auto-fails (409)
  - /tasks/stale lists submitted>10min-without-working and input-required>2h
    in a shape a curl+jq one-liner can consume

Runs two ways:
  pytest blimp-infra/a2a/test_broker.py            # if pytest is available
  python3 blimp-infra/a2a/test_broker.py           # plain assert runner (no pytest)

The broker talks to Postgres via psycopg. We don't stand up a DB — instead we
inject an in-memory fake that understands exactly the queries broker.py issues,
then point broker.db_conn at it. The FastAPI app is exercised via TestClient.
"""

from __future__ import annotations
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# broker.py reads DUKE_DB_URL at import time and (lazily) connects. Give it a
# dummy URL; we replace db_conn before any request runs, so it never dials out.
os.environ.setdefault("DUKE_DB_URL", "postgresql://fake/duke_data")
# Pin thresholds so the stale test is deterministic regardless of host env.
os.environ["A2A_STALE_SUBMITTED_SECS"] = str(10 * 60)
os.environ["A2A_STALE_INPUT_REQ_SECS"] = str(2 * 60 * 60)
os.environ["A2A_BOUNCE_BUDGET"] = "2"

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import broker  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

NOW = lambda: datetime.now(timezone.utc)


# ─── In-memory fake of the slice of Postgres broker.py uses ──────────────
class FakeDB:
    """Holds a2a_messages rows. One instance is shared across the test run so
    state persists between requests, mirroring a real DB."""

    def __init__(self):
        self.rows: list[dict] = []
        self._seq = 0

    def add_root(self, task_id, *, state="submitted", from_agent="duke",
                 to_agent="hermes", skill=None, bounce_count=0,
                 updated_at=None, working_at=None, input_required_at=None):
        self._seq += 1
        row = {
            "id": self._seq, "task_id": task_id, "parent_task_id": None,
            "from_agent": from_agent, "to_agent": to_agent, "state": state,
            "skill": skill, "body": {}, "result": None,
            "bounce_count": bounce_count, "fail_reason": None,
            "created_at": NOW(), "updated_at": updated_at or NOW(),
            "acked_at": None, "working_at": working_at,
            "input_required_at": input_required_at,
            "completed_at": None, "failed_at": None,
        }
        self.rows.append(row)
        return row

    def root(self, task_id):
        for r in self.rows:
            if r["task_id"] == task_id and r["parent_task_id"] is None:
                return r
        return None


_DB = FakeDB()


class FakeCursor:
    def __init__(self, db: FakeDB):
        self.db = db
        self._result: list[dict] = []

    # context-manager protocol (broker uses `with ... as cur`)
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        params = list(params or [])
        s = " ".join(sql.split())  # collapse whitespace for matching

        if s.startswith("ALTER TABLE a2a_messages"):
            return  # migration: no-op against the fake

        if s.startswith("INSERT INTO a2a_messages"):
            self.db._seq += 1
            # broker.insert_message: (task_id, parent, from, to, state, skill, body)
            task_id, parent, frm, to, state, skill, body = params
            self.db.rows.append({
                "id": self.db._seq, "task_id": task_id, "parent_task_id": parent,
                "from_agent": frm, "to_agent": to, "state": state, "skill": skill,
                "body": json.loads(body) if isinstance(body, str) else body,
                "result": None, "bounce_count": 0, "fail_reason": None,
                "created_at": NOW(), "updated_at": NOW(), "acked_at": None,
                "working_at": None, "input_required_at": None,
                "completed_at": None, "failed_at": None,
            })
            self._result = [{"id": self.db._seq}]
            return

        if s.startswith("SELECT * FROM a2a_messages WHERE task_id"):
            row = self.db.root(params[0])
            self._result = [dict(row)] if row else []
            return

        if s.startswith("UPDATE a2a_messages SET"):
            task_id = params[-1]
            row = self.db.root(task_id)
            if not row:
                self._result = []
                return
            # Apply each "col=%s" / "col=NOW()" assignment in order.
            assigns = s[s.index("SET") + 3:s.index("WHERE")].split(",")
            pi = 0
            for a in assigns:
                col, _, val = a.strip().partition("=")
                col = col.strip()
                val = val.strip()
                if val == "%s":
                    v = params[pi]; pi += 1
                    if col == "result" and isinstance(v, str):
                        try:
                            v = json.loads(v)
                        except Exception:
                            pass
                    row[col] = v
                elif val.upper() == "NOW()":
                    row[col] = NOW()
                elif len(val) >= 2 and val[0] == "'" and val[-1] == "'":
                    row[col] = val[1:-1]  # quoted string literal, e.g. state='failed'
                # else: literal we don't model — ignore
            self._result = []
            return

        if s.startswith("SELECT task_id, from_agent, to_agent, state, skill"):
            # The /tasks/stale query. Re-implement its predicate in Python.
            sub_secs, inp_secs = params[0], params[1]
            now = NOW()
            out = []
            for r in self.db.rows:
                if r["parent_task_id"] is not None:
                    continue
                age = (now - r["updated_at"]).total_seconds()
                stale = False
                if (r["state"] == "submitted" and r["working_at"] is None
                        and age > sub_secs):
                    stale = True
                elif r["state"] == "input-required" and age > inp_secs:
                    stale = True
                if stale:
                    out.append({
                        "task_id": r["task_id"], "from_agent": r["from_agent"],
                        "to_agent": r["to_agent"], "state": r["state"],
                        "skill": r["skill"], "bounce_count": r["bounce_count"],
                        "age_secs": int(age),
                    })
            out.sort(key=lambda r: r["age_secs"], reverse=True)
            self._result = out
            return

        raise AssertionError(f"unhandled SQL in fake: {s[:120]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, db: FakeDB):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        pass


def _fake_db_conn():
    return FakeConn(_DB)


# Point the broker at the fake and build a TestClient.
broker.db_conn = _fake_db_conn
client = TestClient(broker.app)


def _reset():
    _DB.rows.clear()
    _DB._seq = 0


GOOD_COMPLETION = {
    "status": "done",
    "artifacts": ["broker.py", "test_broker.py"],
    "verification": {"cmd": "python3 -m py_compile broker.py", "output": "OK"},
}


# ─── Tests ───────────────────────────────────────────────────────────────
def test_migration_runs_clean():
    # startup hook + explicit call should both be no-ops against the fake
    broker.run_migration()


def test_legal_transition_submitted_to_working():
    _reset()
    _DB.add_root("t-legal")
    r = client.post("/tasks/t-legal/transition", json={"to": "working"})
    assert r.status_code == 200, r.text
    assert r.json()["to"] == "working"
    row = _DB.root("t-legal")
    assert row["state"] == "working"
    assert row["working_at"] is not None  # per-transition timestamp stamped


def test_illegal_transition_from_terminal_is_409():
    _reset()
    _DB.add_root("t-term", state="completed")
    r = client.post("/tasks/t-term/transition", json={"to": "working"})
    assert r.status_code == 409, r.text
    assert "terminal" in r.json()["detail"]


def test_unknown_state_is_400():
    _reset()
    _DB.add_root("t-unk")
    r = client.post("/tasks/t-unk/transition", json={"to": "bogus"})
    assert r.status_code == 400, r.text


def test_unknown_task_is_404():
    _reset()
    r = client.post("/tasks/does-not-exist/transition", json={"to": "working"})
    assert r.status_code == 404, r.text


def test_completion_with_good_payload_succeeds():
    _reset()
    _DB.add_root("t-done", state="working")
    r = client.post("/tasks/t-done/transition",
                    json={"to": "completed", "result": GOOD_COMPLETION})
    assert r.status_code == 200, r.text
    row = _DB.root("t-done")
    assert row["state"] == "completed"
    assert row["completed_at"] is not None
    assert row["result"]["status"] == "done"


def test_bad_completion_bounces_then_auto_fails():
    _reset()
    _DB.add_root("t-bounce", state="working")
    bad = {"status": "done"}  # missing artifacts + verification

    # bounce 1 → rejected, task stays open, bounce_count=1
    r1 = client.post("/tasks/t-bounce/transition", json={"to": "completed", "result": bad})
    assert r1.status_code == 422, r1.text
    j1 = r1.json()
    assert j1["bounce_count"] == 1 and j1["auto_failed"] is False
    assert _DB.root("t-bounce")["state"] == "working"  # still open

    # bounce 2 → budget exhausted → auto-fail (409)
    r2 = client.post("/tasks/t-bounce/transition", json={"to": "completed", "result": bad})
    assert r2.status_code == 409, r2.text
    j2 = r2.json()
    assert j2["auto_failed"] is True and j2["state"] == "failed"
    assert j2["bounce_count"] == 2
    row = _DB.root("t-bounce")
    assert row["state"] == "failed"
    assert row["failed_at"] is not None
    assert row["fail_reason"] and "rejected" in row["fail_reason"]


def test_completion_missing_verification_output_is_rejected():
    _reset()
    _DB.add_root("t-vo", state="working")
    bad = {"status": "ok", "artifacts": [], "verification": {"cmd": "ls", "output": ""}}
    r = client.post("/tasks/t-vo/transition", json={"to": "completed", "result": bad})
    assert r.status_code == 422, r.text
    assert any("verification.output" in e for e in r.json()["errors"])


def test_validate_completion_unit():
    assert broker.validate_completion(GOOD_COMPLETION) == []
    assert broker.validate_completion({}) != []
    assert broker.validate_completion("nope") != []
    assert any("artifacts" in e for e in broker.validate_completion({"status": "x", "verification": {"cmd": "c", "output": "o"}}))


def test_stale_lists_submitted_and_input_required():
    _reset()
    now = NOW()
    # submitted, 15 min old, never reached working → stale
    _DB.add_root("s-old", state="submitted",
                 updated_at=now - timedelta(minutes=15), working_at=None)
    # submitted, 2 min old → fresh, not listed
    _DB.add_root("s-fresh", state="submitted",
                 updated_at=now - timedelta(minutes=2), working_at=None)
    # submitted but working_at set (already progressed) → not stale
    _DB.add_root("s-working", state="submitted",
                 updated_at=now - timedelta(minutes=30), working_at=now)
    # input-required, 3 h old → stale (escalate)
    _DB.add_root("ir-old", state="input-required",
                 updated_at=now - timedelta(hours=3))
    # input-required, 1 h old → fresh
    _DB.add_root("ir-fresh", state="input-required",
                 updated_at=now - timedelta(hours=1))

    r = client.get("/tasks/stale")
    assert r.status_code == 200, r.text
    j = r.json()
    ids = {t["task_id"] for t in j["tasks"]}
    assert ids == {"s-old", "ir-old"}, ids
    assert j["count"] == 2
    reasons = {t["task_id"]: t["reason"] for t in j["tasks"]}
    assert reasons["s-old"] == "submitted_no_working"
    assert reasons["ir-old"] == "input_required_overdue"
    actions = {t["task_id"]: t["action"] for t in j["tasks"]}
    assert actions["s-old"] == "renotify"
    assert actions["ir-old"] == "escalate_logan"


def test_message_send_still_works():
    """Backward-compat: the legacy JSON-RPC message/send flow is untouched."""
    _reset()
    # tmux/webhook notify is fire-and-forget; it may no-op in the sandbox.
    payload = {
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user",
                               "parts": [{"kind": "text", "text": "hello"}]}},
    }
    r = client.post("/hermes/", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["status"]["state"] == "submitted"
    assert len(_DB.rows) == 1 and _DB.rows[0]["to_agent"] == "hermes"


# ─── plain-python runner (no pytest required) ────────────────────────────
def _run_plain():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_plain())
