"""Deterministic Discord cards (spec §18.1)."""
from __future__ import annotations

from datetime import datetime, timezone

from blimp.mesh.deck.cards import message_card, status_card, task_card

T = {"id": "T-0151", "title": "Telegram intake for Become bot", "state": "WORKING", "owner": "duke",
     "contract": {"acceptance_checks": [{"id": "a"}, {"id": "b"}, {"id": "c"}], "r3_steps": [{"tool": "deploy.prod_deploy", "why": "ship"}]}}
L = {"plan": [{"status": "done", "receipts": ["RC-9001"]}, {"status": "done", "receipts": ["RC-9004"]}, {"status": "done"},
              {"status": "doing"}, {"status": "todo"}], "next_step": "worker retry backoff"}


def test_task_card_matches_spec_template():
    assert task_card(T, L, budget_used_pct=41) == (
        "[T-0151] Telegram intake for Become bot  ·  WORKING  ·  owner Duke  ·  R3 step pending\n"
        "Progress: 3/5 steps · checks 2/3 with receipts [RC-9001, RC-9004]\n"
        "Budget: 41% · Next: worker retry backoff · ETA: not estimated")


def test_card_is_deterministic():
    assert task_card(T, L) == task_card(T, L)


def test_legacy_and_minimal_cards():
    c = task_card({"id": "T-0001", "title": "Get first paying client", "state": "PROPOSED", "owner": "logan", "contract": {"legacy": True}})
    assert "imported from v1, needs a contract" in c and "owner the Boss" in c
    assert "ETA: not estimated" in c


def test_due_at_in_myt_label():
    t = {**T, "due_at": datetime(2026, 10, 14, 10, 30, tzinfo=timezone.utc)}
    assert "ETA: 14 Oct 18:30 MYT" in task_card(t)  # stored UTC, shown in the Boss's clock


def test_message_card_collapses_long_bodies():
    m = {"act": "PROPOSE", "sender": "hermes", "recipients": ["duke"], "purpose": "Pick the intake architecture",
         "parts": [{"text": "x" * 1000}]}
    out = message_card(m)
    assert out.startswith("[PROPOSE] Hermes → Duke · Pick the intake architecture\n")
    assert "(+600 chars, full text on the board)" in out


def test_status_card_orders_open_work():
    tasks = [
        {"id": "T-0003", "title": "c", "state": "PROPOSED", "owner": "duke"},
        {"id": "T-0001", "title": "a", "state": "WORKING", "owner": "duke"},
        {"id": "T-0002", "title": "b", "state": "DONE", "owner": "duke"},
        {"id": "T-0004", "title": "d", "state": "READY", "owner": "hermes", "pinned": True},
    ]
    out = status_card(tasks)
    assert out.splitlines()[0] == "Board: 1 working, 1 ready, 1 proposed"
    assert out.splitlines()[1].startswith("- [T-0004]")  # the Boss's pin first
    assert "T-0002" not in out
    assert status_card([]) == "Board: nothing open."


def test_approval_card_matches_spec_shape():
    from blimp.mesh.deck.cards import approval_card
    c = approval_card({"id": "AP-0102", "tool": "deploy.prod_deploy", "args": {"app": "<your-product>", "ref": "abc123"},
                       "requested_by": "frank", "executor": "frank", "task_id": "T-0151", "why": "ships Telegram intake",
                       "rollback": "redeploy @9f2e1d (runbook RB-03)", "evidence": ["RC-9010", "RC-9012"]})
    assert c.splitlines()[0] == "APPROVAL AP-0102 · prod_deploy <your-product>@abc123 · requested by Frank for T-0151"
    assert "Rollback: redeploy @9f2e1d (runbook RB-03)" in c and "Evidence: RC-9010, RC-9012" in c
    assert c.endswith("Reply: /approve AP-0102 · /reject AP-0102 <why>")
