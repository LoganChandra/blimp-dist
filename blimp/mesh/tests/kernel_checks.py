"""Blimp Crew v2 — Deck kernel tests (spec-conformance; zero external deps beyond stdlib+psycopg mocks).

Covers: event hash chain, contract lint (7 checks), receipts gate, envelope validation,
wake rules, priority scorer, Ballast budget/loop-breaker, Toolgate authorization,
context compiler manifest.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

# blimp/mesh/tests -> repo root is 3 levels up
_repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, _repo)

from blimp.mesh.deck.priority import priority_score, sort_ready_tasks
from blimp.mesh.deck.contract_lint import lint_contract
from blimp.mesh.deck.envelope import validate_envelope, wakes_recipient
from blimp.mesh.deck.router import WakeEvent, evaluate_wakes
from blimp.mesh.deck.ballast import (
    BudgetLimits, TurnUsage, check_turn_limits, task_budget_check,
    loop_breaker_check, TaskSpendTracker, digest_tool_call, kill_switch_check,
)
from blimp.mesh.context.compiler import compile_pack, pack_hash

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS" if cond else "FAIL"), "-", name)


# ── 1. Priority scorer (§9.6) ────────────────────────────────────────────────
check("priority base math", priority_score(5, 3, 1, 3, 2) == 6.0)
check("priority effort floor", priority_score(5, 3, 1, 3, 1) == 12.0)  # max(effort,1)=1
try:
    priority_score(4, 3, 1, 3, 2); check("priority rejects invalid fib score", False)
except ValueError:
    check("priority rejects invalid fib score", True)
try:
    priority_score(5, 3, 1, 5, 2); check("priority rejects invalid revenue_link", False)
except ValueError:
    check("priority rejects invalid revenue_link", True)
check("pin override +inf", priority_score(1, 1, 1, 0, 8, pinned=True) == float("inf"))
check("incident override +inf", priority_score(1, 1, 1, 0, 8, incident=True) == float("inf"))
soon = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
check("due <48h x1.5", priority_score(2, 2, 2, 0, 2, due_at=soon) == 4.5)

# ── 2. Contract lint (§9.3, all 7 checks) ───────────────────────────────────
GOOD = {
    "id": "T-0142", "title": "Intake webhook", "type": "build",
    "objective": "Webhook intake for WhatsApp", "why": {"mandate_id": "M-0001", "rationale": "client work"},
    "deliverables": [{"name": "PR", "kind": "pr"}],
    "acceptance_checks": [{"id": "c1", "kind": "test", "cmd": "pytest -q tests", "expected": "42 passed"}],
    "definition_of_done": ["PR open, checks green"],
    "owner": "duke", "reviewer": "svc-verifier", "capabilities": ["repo.worktree", "board.read"],
    "boundaries": {"non_goals": ["no BM translation"], "forbidden": ["infra/"], "allowed_paths": ["src/"]},
    "risk_tier": "R1", "budget": {"tokens": 100000, "turns": 5}, "effort_class": "M",
    "worktree": "/home/user/blimp/worktrees/T-0142",
}
ok, errs = lint_contract(GOOD, existing_task_ids={"T-0141"})
check("lint: good contract passes", ok and not errs)

bad = dict(GOOD); bad["acceptance_checks"] = []
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: rejects empty acceptance checks", not ok and any(e.startswith("1:") for e in errs))

bad = dict(GOOD); bad["risk_tier"] = "R0"
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: risk_tier < capability tier rejected", not ok and any(e.startswith("2:") for e in errs))

bad = dict(GOOD); bad["budget"] = {"tokens": 999999, "turns": 99}
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: budget over effort cap rejected", not ok and any(e.startswith("3:") for e in errs))

bad = dict(GOOD); bad["depends_on"] = ["T-9999"]
ok, errs = lint_contract(bad, existing_task_ids={"T-0141"})
check("lint: missing dependency rejected", not ok and any(e.startswith("4:") for e in errs))

bad = dict(GOOD); bad.pop("why")
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: untraceable task rejected", not ok and any(e.startswith("5:") for e in errs))

bad = dict(GOOD)
bad["boundaries"] = {"non_goals": [], "forbidden": ["src/"], "allowed_paths": ["src/app/"]}
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: forbidden/allowed overlap rejected", not ok and any(e.startswith("6:") for e in errs))

bad = dict(GOOD); bad["type"] = "client_delivery"; bad["client"] = {"market": "SG"}
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: client_delivery missing consent rejected", not ok and any(e.startswith("7:") for e in errs))

bad = dict(GOOD); bad["type"] = "maintenance"
ok, errs = lint_contract(bad, existing_task_ids=set())
check("lint: maintenance exempt from traceability", ok)

# ── 3. Receipts gate: moved to tests/test_completion.py (DB-backed gate, 2026-09-30) ──

# ── 4. Envelope validation (§8.3, §8.11) ────────────────────────────────────
def mk_msg(**kw):
    base = {
        "messageId": "01J9Z6N5Y3K8V1M2Q7R4S0T6UX", "contextId": "ctx-T-0142", "taskId": "T-0142",
        "parts": [{"text": "PROPOSE: split intake into webhook + worker"}],
        "metadata": {"https://blimp.my/a2a/ext/speech-act/v1": {
            "act": "PROPOSE", "from": "hermes", "to": ["duke"], "expects": "decision",
            "priority": "P2", "purpose": "Pick the intake architecture",
            "reply_by": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        }},
    }
    base["metadata"]["https://blimp.my/a2a/ext/speech-act/v1"].update(kw)
    return base

ok, errs = validate_envelope(mk_msg(), known_agents={"duke", "hermes", "frank"})
check("envelope: valid PROPOSE passes", ok)

ok, errs = validate_envelope(mk_msg(act="ACK"), known_agents={"duke"})
check("envelope: invalid act rejected", not ok)

ok, errs = validate_envelope(mk_msg(to=["other-agent"]), known_agents={"duke"})
check("envelope: unknown recipient rejected", not ok)

ok, errs = validate_envelope(mk_msg(expects="decision", reply_by=None), known_agents={"duke"})
check("envelope: expects!=none requires reply_by", not ok)

big = mk_msg(); big["parts"] = [{"text": "x" * 20000}]
ok, errs = validate_envelope(big, known_agents={"duke"})
check("envelope: oversized body rejected", not ok and any("artifact" in e for e in errs))

ack = mk_msg(act="ANSWER", expects="none"); ack["parts"] = [{"text": "Got it, thanks"}]
ack["metadata"]["https://blimp.my/a2a/ext/speech-act/v1"].pop("reply_by")
ok, errs = validate_envelope(ack, known_agents={"duke"})
check("envelope: ack messages rejected", not ok and any("acknowledgement" in e for e in errs))

check("wakes: INFORM never wakes", wakes_recipient("INFORM") is False)
check("wakes: STATUS never wakes", wakes_recipient("STATUS") is False)
check("wakes: REQUEST wakes", wakes_recipient("REQUEST") is True)

# ── 5. Wake rules (§8.6) ─────────────────────────────────────────────────────
evs = [
    WakeEvent(1, "message.sent", "logan", {"message": mk_msg(act="REQUEST", to=["duke"], priority="P1")}),
    WakeEvent(2, "message.sent", "hermes", {"message": mk_msg(act="STATUS", to=["duke"])}),
    WakeEvent(3, "alert.raised", "system", {"severity": "sev1"}),
    WakeEvent(4, "verification.failed", "verifier", {"owner": "duke", "task_id": "T-1"}),
]
reqs = evaluate_wakes(evs)
p0 = [r for r in reqs if r.priority == "P0" and r.agent == "duke"]
sev = [r for r in reqs if r.agent == "frank" and r.priority == "P0"]
vfail = [r for r in reqs if r.priority == "P1" and r.agent == "duke" and 4 in r.event_ids]
check("wake: the Boss message -> P0", any(1 in r.event_ids for r in p0))
check("wake: STATUS no wake", not any(2 in r.event_ids for r in reqs))
check("wake: sev1 alert -> frank P0", len(sev) == 1)
check("wake: verification.failed -> owner P1", len(vfail) == 1)

# ── 6. Ballast (§14.5) ───────────────────────────────────────────────────────
limits = {"max_input_tokens": 1000, "max_output_tokens": 500, "max_tool_calls": 10, "max_wall_clock_s": 60}
check("ballast: within limits -> None", check_turn_limits(limits, TurnUsage(500, 100, 5, 30)) is None)
check("ballast: input over -> flagged", check_turn_limits(limits, TurnUsage(2000, 100, 5, 30)) == "max_input_tokens")

budgets = BudgetLimits.from_policy({
    "global_monthly_usd": 100.0, "split": {"duke": 0.4},
    "per_turn": limits, "per_task_by_effort": {"S": {"tokens": 150000, "turns": 6}, "M": {"tokens": 600000, "turns": 20}},
    "huddle": {"max_rounds": 3, "max_tokens": 60000},
    "thresholds": {"warn": 0.8, "hard_stop": 1.0},
    "loop_breaker": {"identical_tool_call_repeats": 3, "unchanged_ledger_turns": 3, "error_streak": 5},
    "kill_switch": {"daily_spend_spike_multiplier": 3.0},
})
check("ballast: task ok", task_budget_check("S", budgets.per_task_by_effort, 50000, 2, budgets.thresholds, True) == "ok")
check("ballast: 80% without progress -> replan", task_budget_check("S", budgets.per_task_by_effort, 130000, 2, budgets.thresholds, False) == "replan")
check("ballast: hard stop", task_budget_check("S", budgets.per_task_by_effort, 160000, 2, budgets.thresholds, False) == "hard_stop")

tracker = TaskSpendTracker()
d = digest_tool_call("repo.run", {"cmd": "pytest"})
tracker.tool_call_digests = [d, d, d]
check("ballast: identical tool calls x3 -> loop", loop_breaker_check(tracker, budgets.loop_breaker) == "pause_task_and_alert_frank")
tracker2 = TaskSpendTracker(); tracker2.error_streak = 5
check("ballast: error streak -> loop", loop_breaker_check(tracker2, budgets.loop_breaker) is not None)
check("ballast: healthy -> no loop", loop_breaker_check(TaskSpendTracker(), budgets.loop_breaker) is None)
check("ballast: spend spike kill switch", kill_switch_check(30.0, 5.0, 3.0))
check("ballast: normal spend no freeze", not kill_switch_check(6.0, 5.0, 3.0))

# ── 7. Toolgate: moved to tests/test_toolgate.py (persisted gate, 2026-09-30) ──

# ── 8. Context Compiler manifest (§11.6, fixes F3/L-017) ─────────────────────
pack = compile_pack(
    {"constitution": "<constitution>C1..C8</constitution>", "identity": "<identity>Duke</identity>",
     "protocol": "<protocol>acts</protocol>", "playbook": "<playbook>skills</playbook>"},
    knowledge_pages={"now": "Top tasks", "decisions-active": "D-008"},
    task_contract="contract json", ledger="ledger v7", thread_tail="last msgs",
    trigger_batch="event batch",
)
layers = [m["layer"] for m in pack.manifest]
check("compiler: L0 first (cache order)", layers[0] == "L0")
check("compiler: manifest has hashes", all("sha256" in m for m in pack.manifest))
check("compiler: L7 trigger batch present", "L7" in layers)
check("compiler: pack hash stable", pack_hash(pack) == pack_hash(pack))

# ── Summary ──────────────────────────────────────────────────────────────────
print(f"\n{'='*50}\n{len(PASS)} passed, {len(FAIL)} failed, {len(PASS)+len(FAIL)} total")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
print("ALL GREEN")
