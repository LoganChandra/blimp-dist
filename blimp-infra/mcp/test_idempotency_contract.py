"""Run: ~/blimp/mesh-venv/bin/pytest blimp-infra/mcp/test_idempotency_contract.py"""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contract  # noqa: E402
import idempotency  # noqa: E402


@pytest.fixture(autouse=True)
def tmpdir(tmp_path, monkeypatch):
    monkeypatch.setattr(idempotency, "DIR", tmp_path)


def test_same_key_acts_once():
    calls = []
    fn = lambda: (calls.append(1), json.dumps({"ok": True, "task_id": "t1"}))[1]
    a = json.loads(idempotency.once("duke", "a2a_send", "k1", fn))
    b = json.loads(idempotency.once("duke", "a2a_send", "k1", fn))
    assert len(calls) == 1 and a["task_id"] == b["task_id"] == "t1" and b["idempotent_replay"] and "idempotent_replay" not in a


def test_no_key_always_acts_and_keys_are_per_tool_and_agent():
    calls = []
    fn = lambda: (calls.append(1), json.dumps({"ok": True}))[1]
    idempotency.once("duke", "send_discord", None, fn)
    idempotency.once("duke", "send_discord", "", fn)
    idempotency.once("duke", "send_discord", "k", fn)
    idempotency.once("duke", "board_post", "k", fn)
    idempotency.once("hermes", "send_discord", "k", fn)
    assert len(calls) == 5


@pytest.mark.parametrize("bad", ['{"ok": false, "error": "x"}', '{"error": "down"}', "not json"])
def test_failures_are_not_remembered(bad):
    calls = []
    fn = lambda: (calls.append(1), bad)[1]
    idempotency.once("duke", "send_discord", "k", fn)
    idempotency.once("duke", "send_discord", "k", fn)
    assert len(calls) == 2


def test_expired_keys_act_again(monkeypatch):
    calls = []
    fn = lambda: (calls.append(1), json.dumps({"ok": True}))[1]
    idempotency.once("duke", "x", "k", fn)
    t = idempotency.time.time()
    monkeypatch.setattr(idempotency.time, "time", lambda: t + idempotency.TTL + 1)
    idempotency.once("duke", "x", "k", fn)
    assert len(calls) == 2


def test_concurrent_callers_act_once():
    calls = []
    def fn():
        calls.append(1)
        return json.dumps({"ok": True})
    ts = [threading.Thread(target=idempotency.once, args=("duke", "x", "same", fn)) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1


def test_corrupt_store_is_ignored(tmp_path):
    (tmp_path / "idempotency-duke.json").write_text("{garbage")
    assert json.loads(idempotency.once("duke", "x", "k", lambda: '{"ok": true}'))["ok"]


def test_contract_renders_all_five_fields_in_order():
    t = contract.render(objective="ship X", output="PR link", boundaries="no prod",
                        stop_condition="tests green", check_command="pytest -q")
    lines = t.splitlines()
    assert lines[0] == "TASK CONTRACT" and len(lines) == 6 and lines[-1].endswith("pytest -q")


@pytest.mark.parametrize("missing", contract.FIELDS)
def test_contract_refuses_a_missing_field(missing):
    f = {k: "x" for k in contract.FIELDS}
    f[missing] = "  "
    with pytest.raises(ValueError, match=missing):
        contract.render(**f)


def test_reused_key_with_different_args_is_refused_not_replayed():
    calls = []
    fn = lambda: (calls.append(1), json.dumps({"ok": True, "task_id": "t1"}))[1]
    idempotency.once("duke", "a2a_send", "status", fn, args=["frank", "first"])
    again = json.loads(idempotency.once("duke", "a2a_send", "status", fn, args=["frank", "first"]))
    other = json.loads(idempotency.once("duke", "a2a_send", "status", fn, args=["frank", "second"]))
    assert len(calls) == 1 and again["idempotent_replay"] and other["ok"] is False and "different" in other["error"]
