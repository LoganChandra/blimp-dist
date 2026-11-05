"""Board tools for the v1 agents (spec §23 phase 1: "agents still v1, reading and writing
the board through new brain-MCP tools"; §23.1 Minimum Lovable v2).

Plain functions returning JSON strings, registered as MCP tools in
blimp-infra/mcp/blimp_brain_mcp.py. The caller's identity comes from the MCP process
environment (A2A_AGENT_NAME), never from a tool argument, so an agent cannot speak as
another agent (spec §14.1 identity spoofing).

Tool descriptions follow spec §13.2: what it does, when to use it, when not to, the
output shape, and validation errors returned as results (not exceptions) so the model
can correct itself.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from ..deck.board import Board, BoardError

# nanook is Frank's wiring name (D-002). Anything else unknown is refused by the board.
WIRING_TO_PRINCIPAL = {"nanook": "frank", "frank": "frank", "duke": "duke", "hermes": "hermes"}
EXT = "https://blimp.my/a2a/ext/speech-act/v1"
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def whoami() -> str:
    raw = os.environ.get("A2A_AGENT_NAME", "").strip().lower()
    return WIRING_TO_PRINCIPAL.get(raw, raw)


def ulid() -> str:
    """26-char Crockford ULID: 48-bit ms timestamp + 80 random bits (sortable message ids)."""
    t = int(time.time() * 1000)
    r = int.from_bytes(secrets.token_bytes(10), "big")
    n = (t << 80) | r
    return "".join(_ULID_ALPHABET[(n >> (5 * i)) & 31] for i in reversed(range(26)))


def _ok(obj: Any) -> str:
    return json.dumps({"ok": True, **(obj if isinstance(obj, dict) else {"result": obj})}, default=str)


def _err(e: Exception) -> str:
    if isinstance(e, BoardError):
        return json.dumps({"ok": False, **e.as_dict()}, default=str)
    return json.dumps({"ok": False, "error": "unexpected", "message": str(e)[:500]})


def _json_arg(value: str | dict, name: str) -> dict:
    if isinstance(value, dict):
        return value
    try:
        v = json.loads(value)
    except (TypeError, ValueError) as e:
        raise BoardError("bad_json", f"{name} must be a JSON object ({e})")
    if not isinstance(v, dict):
        raise BoardError("bad_json", f"{name} must be a JSON object")
    return v


def _board() -> Board:
    return Board(os.environ.get("BLIMPG_DSN"))


# ── R0 reads ──────────────────────────────────────────────────────────────
def board_tasks(owner: str | None = None, state: str | None = None, limit: int = 30) -> str:
    """List board tasks (newest first; the Boss's pins first). Filter by owner and/or a comma-separated
    state list (e.g. "READY,WORKING"). Use before proposing new work: the task may already exist.
    Returns {ok, tasks:[{id,title,type,state,owner,priority,pinned,updated_at}]}."""
    try:
        states = [s.strip().upper() for s in state.split(",")] if state else None
        return _ok({"tasks": _board().list_tasks(owner=owner, states=states, limit=min(int(limit), 100))})
    except Exception as e:
        return _err(e)


def board_task(task_id: str) -> str:
    """One task in full: contract, state (+ its A2A state), lease, and the current ledger.
    Use to resume work or to check a teammate's task before messaging them. Returns {ok, task, ledger}."""
    try:
        b = _board()
        return _ok({"task": b.get_task(task_id), "ledger": b.ledger(task_id)})
    except Exception as e:
        return _err(e)


def board_inbox(unread_only: bool = True, limit: int = 20) -> str:
    """Your messages, most urgent first (P0..P3), and mark them read (read receipts are set here,
    by code; never send "got it"). Check at session start and before you finish.
    Returns {ok, messages:[{id,task_id,from,act,priority,purpose,text,reply_by}]}."""
    try:
        me = whoami()
        b = _board()
        msgs = b.inbox(me, unread_only=unread_only, limit=min(int(limit), 50))
        out = [{**{k: m[k] for k in ("id", "task_id", "from", "act", "priority", "purpose", "reply_by", "created_at")},
                "text": " ".join(p.get("text", "") for p in (m["parts"] or []) if isinstance(p, dict))} for m in msgs]
        if msgs:
            b.mark_read([m["id"] for m in msgs], me)
        return _ok({"messages": out})
    except Exception as e:
        return _err(e)


# ── R2 writes ─────────────────────────────────────────────────────────────
def board_task_new(title: str, type: str, mandate_id: str = "", objective_id: str = "", owner: str = "") -> str:
    """Create a DRAFT task (T-####) with its own thread. Every task traces to a mandate (M-####, the Boss's
    words) or an objective, unless type is maintenance or incident. Owner defaults by type (design/research
    -> hermes, build/fix -> duke, ops/incident/report -> frank). Do NOT use for work that already has a task.
    Returns {ok, id, context_id, state, owner}."""
    try:
        return _ok(_board().create_task(title=title, type=type, created_by=whoami(), owner=owner or None,
                                        mandate_id=mandate_id or None, objective_id=objective_id or None))
    except Exception as e:
        return _err(e)


def board_task_propose(task_id: str, contract_json: str) -> str:
    """Attach a v2 task contract (spec 16.2) to a DRAFT/PROPOSED task. The lint runs immediately and its
    errors come back so you can fix them; a failing contract stays PROPOSED. Contracts freeze at READY.
    Returns {ok, id, state, lint_ok, lint_errors[]}."""
    try:
        return _ok(_board().propose(task_id, _json_arg(contract_json, "contract_json"), whoami()))
    except Exception as e:
        return _err(e)


def board_task_accept(task_id: str, reviewed_version: int) -> str:
    """Owner only: ACCEPT a PROPOSED task into READY. Pass the task `version` you read with board_task;
    if the contract changed since, the accept is refused so you never freeze a contract you did not see.
    Refused if the contract fails lint (errors returned). Returns {ok, id, state}."""
    try:
        return _ok(_board().make_ready(task_id, whoami(), reviewed_version=int(reviewed_version)))
    except Exception as e:
        return _err(e)


def board_task_claim(task_id: str) -> str:
    """Owner only: start a READY task (READY -> WORKING) and take its lease (the contract's
    budget.wall_clock_h, default 4 hours). Refused while any dependency is not DONE.
    Returns {ok, id, state, lease_expires_at}."""
    try:
        return _ok(_board().claim(task_id, whoami()))
    except Exception as e:
        return _err(e)


def board_task_renew(task_id: str, hours: float = 4) -> str:
    """Lease holder: extend your lease on a WORKING task (max 12 hours from now). An expired lease sends
    the task back to READY and alerts Frank, and blocks your ledger writes and moves until you re-claim.
    Returns {ok, lease_expires_at}."""
    try:
        h = max(0.25, min(float(hours), 12.0))
        return _ok({"lease_expires_at": _board().renew_lease(task_id, whoami(), ttl=timedelta(hours=h))})
    except Exception as e:
        return _err(e)


def board_task_move(task_id: str, to_state: str, reason: str) -> str:
    """Lease holder: move your WORKING task to INPUT_REQUIRED, AUTH_REQUIRED, BLOCKED, PARKED, FAILED or
    CANCELED (or back to WORKING/READY where the lifecycle allows). Always give the reason. READY, IN_REVIEW
    and DONE have their own gates and are refused here. Returns {ok, id, state}."""
    try:
        return _ok(_board().transition(task_id, to_state.strip().upper(), whoami(), reason))
    except Exception as e:
        return _err(e)


def board_ledger(task_id: str, ledger_json: str) -> str:
    """Lease holder: write the task ledger (your resume point; spec 9.8). Required keys:
    objective_restated, plan[] (steps with status), progress {is_complete, is_progressing, evidence},
    next_step. Update it at the end of every turn. is_looping is set by the system, not you.
    Returns {ok, version}."""
    try:
        return _ok({"version": _board().ledger_update(task_id, whoami(), _json_arg(ledger_json, "ledger_json"))})
    except Exception as e:
        return _err(e)


def board_send(to: str, act: str, text: str, task_id: str, purpose: str, expects: str = "none",
               reply_within_minutes: int = 0, priority: str = "P2", cc: str = "", in_reply_to: str = "",
               new_task_title: str = "", new_task_type: str = "", new_task_mandate_id: str = "") -> str:
    """Send one typed message to teammates (comma-separated `to`) about a task. Acts: REQUEST, QUERY,
    ANSWER, PROPOSE, ACCEPT, COUNTER, REJECT, PUSHBACK, STATUS, ALERT, INFORM, ESCALATE (HANDOFF and DONE
    come in phase 2). One intent per message; the first line states it. No "got it"/"thanks" (rejected).
    expects != none needs reply_within_minutes. New work: task_id="new" plus new_task_title,
    new_task_type and new_task_mandate_id. Over ~1,500 tokens: write an artifact and send its reference.
    Returns {ok, id, task_id, warning}."""
    try:
        me = whoami()
        b = _board()
        recipients = [r.strip() for r in to.split(",") if r.strip()]
        sa: dict[str, Any] = {"act": act.strip().upper(), "from": me, "to": recipients, "expects": expects,
                              "priority": priority, "purpose": purpose}
        if cc:
            sa["cc"] = [c.strip() for c in cc.split(",") if c.strip()]
        if in_reply_to:
            sa["in_reply_to"] = in_reply_to
        if reply_within_minutes:
            sa["reply_by"] = (datetime.now(timezone.utc) + timedelta(minutes=int(reply_within_minutes))).isoformat()
        if task_id == "new":
            sa["new_task"] = {"title": new_task_title or purpose[:90], "type": new_task_type or "build",
                              "mandate_id": new_task_mandate_id or None}
            ctx = "new"
        else:
            ctx = b.get_task(task_id)["context_id"]
        message = {"messageId": ulid(), "contextId": ctx, "taskId": task_id, "role": "ROLE_AGENT",
                   "parts": [{"text": text}], "metadata": {EXT: sa}}
        return _ok(b.send(message))
    except Exception as e:
        return _err(e)


def _gate():
    from ..toolgate.checks import CheckRunner
    from ..toolgate.gate import Toolgate

    dsn = os.environ.get("BLIMPG_DSN")
    return Toolgate(dsn, {"check.run": CheckRunner(dsn or _board().dsn).adapter()})


def board_check_run(task_id: str, check_id: str) -> str:
    """Run one of your task's acceptance checks through Toolgate and get a receipt (RC-####). Only the
    check id: the command and the expected result come from the frozen contract. Cite the receipt in
    board_submit_completion. A failing check returns ok=false with the runner's own summary.
    Returns {ok, receipt_id, passed, summary}."""
    try:
        g = _gate()
        turn = g.open_turn(whoami(), task_id)
        r = g.call(turn, whoami(), "check.run", {"check_id": check_id})
        passed = bool(r.result and r.result.get("passed")) if isinstance(r.result, dict) else False
        return json.dumps({"ok": r.allowed, "receipt_id": r.receipt_id, "passed": passed, "summary": r.reason, "code": r.code}, default=str)
    except Exception as e:
        return _err(e)


def board_submit_completion(task_id: str, report_json: str) -> str:
    """Lease holder: report DONE (spec 16.5 CompletionReport: task_id, summary, claims[{text, receipts[]}],
    deliverables[{name, ref}], checks[{check_id, receipt, result}], known_gaps[], rollback). The receipts
    gate rejects it, with the exact problems, unless every claim and every runnable check is backed by a
    valid check.run receipt. On success the verifier re-runs the checks itself. Returns {ok, id, state}."""
    try:
        return _ok(_board().submit_completion(task_id, whoami(), _json_arg(report_json, "report_json")))
    except Exception as e:
        return _err(e)


def board_request_approval(task_id: str, tool: str, args_json: str, why: str, rollback: str, evidence: str = "") -> str:
    """Ask the Boss to approve ONE exact R3 action (merge to main, prod deploy, client/public message, spending,
    secrets, DNS, restore). The token is bound to this tool + these exact args, single-use, 24h, executed by
    Frank. Give the rollback and cite receipts in evidence (comma-separated RC ids). Returns {ok, id, status}."""
    try:
        return _ok(_gate().request_approval(requested_by=whoami(), executor="frank", tool=tool, args=_json_arg(args_json, "args_json"),
                                            why=why, rollback=rollback, evidence=[e.strip() for e in evidence.split(",") if e.strip()],
                                            task_id=task_id))
    except Exception as e:
        return _err(e)


def board_mandate(verbatim: str, source: str) -> str:
    """Record the Boss's words EXACTLY as he wrote them (C5): copy, never paraphrase, keep his spelling.
    source = where he said it (e.g. "discord:#nanook:<message id>"). Returns {ok, id} (M-####), which
    tasks cite in why.mandate_id."""
    try:
        return _ok({"id": _board().record_mandate(verbatim, source, recorded_by=whoami())})
    except Exception as e:
        return _err(e)


ALL = [board_tasks, board_task, board_inbox, board_task_new, board_task_propose, board_task_accept,
       board_task_claim, board_task_renew, board_task_move, board_ledger, board_send, board_mandate,
       board_check_run, board_submit_completion, board_request_approval]
