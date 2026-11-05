"""Board (spec §9, §8): lifecycle gates, leases, ledgers, messages — happy path and hostile."""
from __future__ import annotations

import os

import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from blimp.mesh.deck.board import A2A_STATE, Board, BoardError, STATES, TRANSITIONS
from blimp.mesh.deck.events import EventStore

EXT = "https://blimp.my/a2a/ext/speech-act/v1"


def contract(tid, **over):
    c = {
        "id": tid, "title": "Webhook intake", "type": "build",
        "objective": "Webhook intake for WhatsApp messages behind a queue.",
        "why": {"mandate_id": "M-0001", "rationale": "the Boss asked"},
        "deliverables": [{"name": "PR", "kind": "pr"}],
        "acceptance_checks": [{"id": "c1", "kind": "test", "cmd": "pytest -q", "expected": "exit 0"}],
        "definition_of_done": ["PR open"], "owner": "duke", "reviewer": "svc-verifier",
        "capabilities": ["repo.read", "repo.worktree"],
        "boundaries": {"non_goals": [], "forbidden": []},
        "risk_tier": "R1", "budget": {"tokens": 100000, "turns": 5}, "effort_class": "S",
        "worktree": os.path.join(os.environ["BLIMP_WORKTREE_ROOTS"].split(":")[0], tid),
    }
    c.update(over)
    return c


@pytest.fixture()
def board(fresh_db):
    b = Board(fresh_db)
    b.record_mandate("build the intake please", source="discord:test")  # M-0001
    return b


def ready_task(b, **over):
    t = b.create_task(title="Webhook intake", type="build", created_by="hermes", mandate_id="M-0001")
    b.propose(t["id"], contract(t["id"], **over), "hermes")
    b.make_ready(t["id"], "duke")
    return t["id"]


def msg(b_task, frm="hermes", to=("duke",), act="QUERY", text="QUERY: retention period for raw payloads?", **sa):
    tid, ctx = b_task
    meta = {"act": act, "from": frm, "to": list(to), "expects": "answer" if act == "QUERY" else "none",
            "priority": "P2", "purpose": "retention question"}
    if meta["expects"] != "none":
        meta["reply_by"] = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    meta.update(sa)
    return {"messageId": uuid.uuid4().hex, "contextId": ctx, "taskId": tid, "role": "ROLE_AGENT",
            "parts": [{"text": text}], "metadata": {EXT: meta}}


# ── state machine is exactly spec 9.4 ─────────────────────────────────────────
def test_state_machine_matches_spec():
    assert set(A2A_STATE) == STATES
    assert TRANSITIONS["READY"] == {"WORKING"}
    assert "DONE" not in TRANSITIONS  # terminal
    assert A2A_STATE["PARKED"] == "WORKING" and A2A_STATE["DONE"] == "COMPLETED"


def test_ids_are_sequential_and_human(board):
    a = board.create_task(title="a", type="maintenance", created_by="frank")
    b = board.create_task(title="b", type="maintenance", created_by="frank")
    assert (a["id"], b["id"]) == ("T-0001", "T-0002")
    assert a["context_id"] == "ctx-T-0001"


def test_maintenance_owner_defaults_to_creating_agent(board):
    assert board.create_task(title="x", type="maintenance", created_by="frank")["owner"] == "frank"
    assert board.create_task(title="y", type="maintenance", created_by="logan")["owner"] is None


def test_untraceable_task_refused(board):
    with pytest.raises(BoardError) as e:
        board.create_task(title="vibes", type="build", created_by="duke")
    assert e.value.code == "untraceable"


def test_unknown_creator_refused(board):
    with pytest.raises(BoardError) as e:
        board.create_task(title="x", type="maintenance", created_by="other-agent")
    assert e.value.code == "unknown_principal"


def test_happy_path_to_working(board):
    tid = ready_task(board)
    t = board.claim(tid, "duke")
    assert t["state"] == "WORKING"
    assert board.get_task(tid)["contract_frozen_at"] is not None


def test_bad_contract_cannot_become_ready(board):
    t = board.create_task(title="x", type="build", created_by="hermes", mandate_id="M-0001")
    r = board.propose(t["id"], contract(t["id"], acceptance_checks=[]), "hermes")
    assert not r["lint_ok"]
    with pytest.raises(BoardError) as e:
        board.make_ready(t["id"], "duke")
    assert e.value.code == "lint_failed" and e.value.details
    assert board.get_task(t["id"])["state"] == "PROPOSED"
    types = [ev["type"] for ev in EventStore(board.dsn).events_for_subject("task", t["id"])]
    assert types.count("task.lint_failed") == 2  # at propose and at the refused READY


def test_only_owner_accepts_into_ready(board):
    t = board.create_task(title="x", type="build", created_by="hermes", mandate_id="M-0001")
    board.propose(t["id"], contract(t["id"]), "hermes")
    with pytest.raises(BoardError) as e:
        board.make_ready(t["id"], "hermes")
    assert e.value.code == "not_owner"


def test_contract_frozen_after_ready(board):
    tid = ready_task(board)
    with pytest.raises(BoardError) as e:
        board.propose(tid, contract(tid, objective="sneakier objective"), "hermes")
    assert e.value.code == "bad_transition"


def test_cannot_skip_the_gates(board):
    tid = ready_task(board)
    board.claim(tid, "duke")
    for to in ("IN_REVIEW", "DONE"):
        with pytest.raises(BoardError):
            board.transition(tid, to, "duke", "skip")
    with pytest.raises(BoardError) as e:
        board.transition(tid, "PROPOSED", "duke", "back")
    assert e.value.code == "bad_transition"


def test_only_owner_claims(board):
    tid = ready_task(board)
    with pytest.raises(BoardError) as e:
        board.claim(tid, "hermes")
    assert e.value.code == "not_owner"
    assert board.claim(tid, "duke:pairing")["state"] == "WORKING"  # pairing mode takes the owner's lease


def test_double_claim_race_has_one_winner(board):
    tid = ready_task(board)
    results = []

    def go():
        try:
            board.claim(tid, "duke")
            results.append("won")
        except BoardError as e:
            results.append(e.code)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("won") == 1 and set(results) - {"won"} == {"bad_transition"}


def test_dependency_must_be_done_before_claim(board):
    dep = ready_task(board)
    t = board.create_task(title="after", type="build", created_by="hermes", mandate_id="M-0001")
    board.propose(t["id"], contract(t["id"], depends_on=[dep]), "hermes")
    board.make_ready(t["id"], "duke")
    with pytest.raises(BoardError) as e:
        board.claim(t["id"], "duke")
    assert e.value.code == "dependencies_open" and e.value.details == [dep]


def test_done_needs_review_and_verifier_and_unblocks_dependents(board):
    dep = ready_task(board)
    t = board.create_task(title="after", type="build", created_by="hermes", mandate_id="M-0001")
    board.propose(t["id"], contract(t["id"], depends_on=[dep]), "hermes")
    board.make_ready(t["id"], "duke")
    board.claim(dep, "duke")
    with pytest.raises(BoardError):
        board.mark_done(dep, "svc-verifier", {"verdict": "PASS"})  # not IN_REVIEW
    with psycopg.connect(board.dsn) as c:  # IN_REVIEW is entered through the receipts gate (phase 2); simulate it
        c.execute("UPDATE mesh.tasks SET state='IN_REVIEW' WHERE id=%s", (dep,))
    with pytest.raises(BoardError) as e:
        board.mark_done(dep, "duke", {"verdict": "PASS"})
    assert e.value.code == "not_verifier"  # the builder never verifies his own work
    with pytest.raises(BoardError):
        board.mark_done(dep, "svc-verifier", {"verdict": "FAIL"})
    board.mark_done(dep, "svc-verifier", {"verdict": "PASS"})
    ev = [e for e in EventStore(board.dsn).events_for_subject("task", t["id"]) if e["type"] == "task.unblocked"]
    assert len(ev) == 1 and ev[0]["payload"]["owner"] == "duke"
    assert board.claim(t["id"], "duke")["state"] == "WORKING"


def test_lease_expiry_returns_task_and_alerts(board):
    tid = ready_task(board)
    board.claim(tid, "duke", ttl=timedelta(seconds=-1))
    assert board.expire_leases() == [tid]
    t = board.get_task(tid)
    assert t["state"] == "READY" and t["lease_holder"] is None
    ev = [e for e in EventStore(board.dsn).events_for_subject("task", tid) if e["type"] == "lease.expired"]
    assert ev and ev[0]["payload"]["alert"] == "frank"
    with pytest.raises(BoardError) as e:
        board.renew_lease(tid, "duke")
    assert e.value.code == "not_lease_holder"


def test_renew_by_non_holder_refused(board):
    tid = ready_task(board)
    board.claim(tid, "duke")
    with pytest.raises(BoardError):
        board.renew_lease(tid, "frank")
    assert board.renew_lease(tid, "duke") > datetime.now(timezone.utc)


def test_transition_rules(board):
    tid = ready_task(board)
    board.claim(tid, "duke")
    with pytest.raises(BoardError) as e:
        board.transition(tid, "BLOCKED", "hermes", "not mine")
    assert e.value.code == "not_lease_holder"
    board.transition(tid, "BLOCKED", "duke", "waiting on Meta app review")
    assert board.get_task(tid)["lease_holder"] is None
    board.transition(tid, "READY", "duke", "unblocked")
    board.claim(tid, "duke")
    board.transition(tid, "CANCELED", "logan", "not needed")  # the Boss can always stop work
    assert board.get_task(tid)["state"] == "CANCELED"
    with pytest.raises(BoardError):
        board.transition(tid, "READY", "logan", "undo")  # terminal is terminal


# ── ledgers ─────────────────────────────────────────────────────────────────
LEDGER = {"objective_restated": "intake", "plan": [{"step": 1, "what": "scaffold", "status": "doing"}],
          "progress": {"is_complete": False, "is_progressing": True}, "next_step": "scaffold"}


def test_ledger_versioning_and_history(board):
    tid = ready_task(board)
    board.claim(tid, "duke")
    assert board.ledger_update(tid, "duke", LEDGER) == 1
    assert board.ledger_update(tid, "duke", {**LEDGER, "next_step": "worker"}) == 2
    assert board.ledger(tid)["next_step"] == "worker"
    with psycopg.connect(board.dsn) as c:
        assert c.execute("SELECT version, body->>'next_step' FROM mesh.ledger_history WHERE task_id=%s", (tid,)).fetchall() == [(1, "scaffold")]


def test_only_lease_holder_writes_ledger(board):
    tid = ready_task(board)
    board.claim(tid, "duke")
    with pytest.raises(BoardError) as e:
        board.ledger_update(tid, "hermes", LEDGER)
    assert e.value.code == "not_lease_holder"


def test_agent_cannot_set_is_looping(board):
    tid = ready_task(board)
    board.claim(tid, "duke")
    board.ledger_update(tid, "duke", {**LEDGER, "progress": {"is_complete": False, "is_looping": False}})
    assert "is_looping" not in board.ledger(tid)["progress"]
    board.set_looping(tid, True)
    board.ledger_update(tid, "duke", {**LEDGER, "progress": {"is_looping": False}})  # agent tries to clear it
    assert board.ledger(tid)["progress"]["is_looping"] is True


@pytest.mark.parametrize("missing", ["objective_restated", "plan", "progress", "next_step"])
def test_ledger_requires_fields(board, missing):
    tid = ready_task(board)
    board.claim(tid, "duke")
    bad = {k: v for k, v in LEDGER.items() if k != missing}
    with pytest.raises(BoardError) as e:
        board.ledger_update(tid, "duke", bad)
    assert e.value.code == "ledger_invalid" and missing in e.value.details


# ── messages ────────────────────────────────────────────────────────────────
@pytest.fixture()
def task_ctx(board):
    tid = ready_task(board)
    return tid, f"ctx-{tid}"


def test_send_and_read_receipts(board, task_ctx):
    m = msg(task_ctx)
    r = board.send(m)
    assert r["id"] == m["messageId"] and r["warning"] is None
    inbox = board.inbox("duke")
    assert [i["id"] for i in inbox] == [m["messageId"]]
    assert board.mark_read([m["messageId"]], "hermes") == 0  # not a recipient
    assert board.mark_read([m["messageId"]], "duke") == 1
    assert board.inbox("duke") == []


def test_send_is_idempotent(board, task_ctx):
    m = msg(task_ctx)
    board.send(m)
    assert board.send(m)["duplicate"] is True
    with psycopg.connect(board.dsn) as c:
        assert c.execute("SELECT count(*) FROM mesh.messages").fetchone()[0] == 1


def test_context_must_match_task(board, task_ctx):
    m = msg(task_ctx)
    m["contextId"] = "ctx-T-9999"
    with pytest.raises(BoardError) as e:
        board.send(m)
    assert any("does not match" in d for d in e.value.details)


def test_nonexistent_task_rejected(board, task_ctx):
    m = msg(("T-9999", "ctx-T-9999"))
    with pytest.raises(BoardError) as e:
        board.send(m)
    assert any("does not exist" in d for d in e.value.details)


def test_forged_sender_rejected(board, task_ctx):
    with pytest.raises(BoardError) as e:
        board.send(msg(task_ctx, frm="other-agent"))
    assert e.value.code == "unknown_principal"


def test_ack_rejected(board, task_ctx):
    with pytest.raises(BoardError):
        board.send(msg(task_ctx, act="INFORM", text="Got it, thanks!"))


def test_one_intent_warn_then_reject(board, task_ctx):
    two = "PROPOSE: use a queue.\nQUERY: what's the retention period?"
    r = board.send(msg(task_ctx, act="PROPOSE", text=two, expects="decision",
                       reply_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
    assert r["warning"] and "next time" in r["warning"]
    with pytest.raises(BoardError) as e:
        board.send(msg(task_ctx, act="PROPOSE", text=two, expects="decision",
                       reply_by=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()))
    assert any("one intent" in d for d in e.value.details)


def test_request_for_new_work_creates_a_proposed_task(board, task_ctx):
    m = msg(task_ctx, act="REQUEST", text="REQUEST: add Telegram intake", expects="accept",
            reply_by=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
            new_task={"title": "Telegram intake", "type": "build", "mandate_id": "M-0001"})
    m["taskId"] = "new"
    r = board.send(m)
    t = board.get_task(r["task_id"])
    assert t["state"] == "PROPOSED" and t["owner"] == "duke" and t["title"] == "Telegram intake"


def test_bad_new_work_request_leaves_no_orphan(board, task_ctx):
    before = len(board.list_tasks())
    m = msg(task_ctx, act="REQUEST", text="REQUEST: add Telegram intake", expects="accept", reply_by=None,
            new_task={"title": "Telegram intake", "type": "build", "mandate_id": "M-0001"})
    m["taskId"] = "new"
    with pytest.raises(BoardError):
        board.send(m)  # missing reply_by
    m2 = msg(task_ctx, act="REQUEST", text="REQUEST: something", expects="accept",
             reply_by=(datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(), new_task={"title": "untraced", "type": "build"})
    m2["taskId"] = "new"
    with pytest.raises(BoardError) as e:
        board.send(m2)
    assert e.value.code == "untraceable"
    assert len(board.list_tasks()) == before


# ── mandates and decisions (C5) ───────────────────────────────────────────────
def test_mandate_is_stored_verbatim_with_hash(board):
    words = "  keep going. dont touch the 'please' stuff!! 🙂  "
    mid = board.record_mandate(words, source="discord:1")
    with psycopg.connect(board.dsn) as c:
        verbatim, sha = c.execute("SELECT verbatim, sha256 FROM mesh.mandates WHERE id=%s", (mid,)).fetchone()
    import hashlib
    assert verbatim == words and bytes(sha) == hashlib.sha256(words.encode()).digest()


def test_logan_decision_needs_his_words(board):
    with pytest.raises(BoardError) as e:
        board.record_decision(title="x", context="c", decision="d", deciders=["logan"], actor="frank")
    assert e.value.code == "verbatim_required"
    did = board.record_decision(title="x", context="c", decision="d", deciders=["logan"], actor="frank", logan_verbatim="B for now")
    assert did == "D-011"  # continues the v1 registry (D-001..D-010)


def test_supersede_marks_the_old_decision(board):
    a = board.record_decision(title="a", context="c", decision="d", deciders=["hermes"], actor="hermes")
    b = board.record_decision(title="b", context="c", decision="d2", deciders=["hermes"], actor="hermes", supersedes=a)
    with psycopg.connect(board.dsn) as c:
        assert c.execute("SELECT status FROM mesh.decisions WHERE id=%s", (a,)).fetchone()[0] == "superseded"
    assert b != a


def test_every_board_change_is_in_the_chain(board, task_ctx):
    tid, _ = task_ctx
    board.claim(tid, "duke")
    ok, broken = EventStore(board.dsn).verify_chain()
    assert ok, broken
    with psycopg.connect(board.dsn) as c:
        n_events = c.execute("SELECT count(*) FROM mesh.events WHERE subject_id=%s", (tid,)).fetchone()[0]
    assert n_events >= 5  # drafted, state_changed x3, proposed, ready, claimed
