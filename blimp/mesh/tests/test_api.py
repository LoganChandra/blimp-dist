"""Deck API (spec §10.1, §8.2): auth, sender binding, A2A facade, status cards."""
from __future__ import annotations

import importlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from .test_board import contract

EXT = "https://blimp.my/a2a/ext/speech-act/v1"


@pytest.fixture()
def client(fresh_db, tmp_path, monkeypatch):
    monkeypatch.setenv("BLIMPG_DSN", fresh_db)
    monkeypatch.setenv("DECK_TOKENS_FILE", str(tmp_path / "tokens.json"))
    import blimp.mesh.deck.api as api
    api = importlib.reload(api)
    toks = {p: api.mint(p) for p in ("duke", "hermes", "frank", "logan")}
    c = TestClient(api.app)
    c.toks = toks
    c.h = lambda p: {"Authorization": f"Bearer {toks[p]}", "A2A-Version": "1.0"}
    from blimp.mesh.deck.board import Board
    Board(fresh_db).record_mandate("build intake", "test")
    return c


def test_token_file_holds_only_hashes(client, tmp_path):
    raw = (tmp_path / "tokens.json").read_text()
    assert all(tok not in raw for tok in client.toks.values())


def test_no_token_no_access(client):
    assert client.get("/tasks").status_code == 401
    assert client.get("/tasks", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health").status_code == 200  # liveness only


def test_no_raw_event_endpoint(client):
    assert client.post("/events", json={"actor": "logan", "type": "approval.granted", "payload": {}}, headers=client.h("duke")).status_code in (404, 405)


def test_board_flow_over_http(client):
    t = client.post("/tasks", json={"title": "Intake", "type": "build", "mandate_id": "M-0001"}, headers=client.h("hermes")).json()
    tid = t["id"]
    r = client.post(f"/tasks/{tid}/propose", json={"contract": contract(tid)}, headers=client.h("hermes")).json()
    assert r["lint_ok"], r
    v = client.get(f"/tasks/{tid}", headers=client.h("duke")).json()["task"]["version"]
    assert client.post(f"/tasks/{tid}/accept?reviewed_version={v}", headers=client.h("hermes")).status_code == 403
    assert client.post(f"/tasks/{tid}/accept?reviewed_version={v}", headers=client.h("duke")).json()["state"] == "READY"
    assert client.post(f"/tasks/{tid}/claim", headers=client.h("duke")).json()["state"] == "WORKING"
    card = client.get(f"/status?task={tid}", headers=client.h("logan")).json()["text"]
    assert card.startswith(f"[{tid}] Intake  ·  WORKING  ·  owner Duke")
    assert client.get("/a2a/duke/tasks/" + tid, headers=client.h("logan")).json()["status"]["state"] == "TASK_STATE_WORKING"
    assert client.post(f"/tasks/{tid}/move", json={"to_state": "DONE", "reason": "trust me"}, headers=client.h("duke")).status_code == 409


def _msg(tid, frm, to):
    return {"messageId": uuid.uuid4().hex, "contextId": f"ctx-{tid}", "taskId": tid, "role": "ROLE_AGENT",
            "parts": [{"text": "QUERY: retention period?"}],
            "metadata": {EXT: {"act": "QUERY", "from": frm, "to": [to], "expects": "answer", "priority": "P1",
                               "purpose": "retention", "reply_by": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}}}


def test_a2a_send_binds_sender_to_token(client):
    tid = client.post("/tasks", json={"title": "Intake", "type": "build", "mandate_id": "M-0001"}, headers=client.h("hermes")).json()["id"]
    forged = _msg(tid, "logan", "duke")  # duke's token, claiming to be the Boss
    r = client.post("/a2a/duke/message:send", json={"message": forged}, headers=client.h("duke"))
    assert r.status_code == 403
    ok = client.post("/a2a/duke/message:send", json={"message": _msg(tid, "hermes", "duke")}, headers=client.h("hermes"))
    assert ok.status_code == 200 and ok.json()["wakes"] == [{"agent": "duke", "priority": "P1"}]
    assert client.post("/a2a/frank/message:send", json={"message": _msg(tid, "hermes", "duke")}, headers=client.h("hermes")).status_code == 400
    no_ver = {"Authorization": client.h("hermes")["Authorization"]}
    assert client.post("/a2a/duke/message:send", json={"message": _msg(tid, "hermes", "duke")}, headers=no_ver).status_code == 400
    inbox = client.get("/inbox", headers=client.h("duke")).json()
    assert len(inbox) == 1 and client.get("/inbox", headers=client.h("duke")).json() == []


def test_errors_map_to_http_codes(client):
    assert client.get("/tasks/T-9999", headers=client.h("duke")).status_code == 404
    bad = client.post("/tasks", json={"title": "x", "type": "build"}, headers=client.h("duke"))
    assert bad.status_code == 400 and bad.json()["error"] == "untraceable"
