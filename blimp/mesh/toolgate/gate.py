"""Blimp Crew v2 — Toolgate: the single policy enforcement point (spec §13).

Every agent tool call passes here. In order:
  0. idempotency replay (same key -> the original receipt, the effect never repeats)
  1. identity: the turn exists, belongs to the caller, has not expired
  2. the tool exists in the catalog
  3. authority matrix: the tool's risk tier <= the caller's maximum (spec 7.4)
  4. least privilege: the task contract's capabilities cover the tool (spec 9.3); a turn
     with no task gets the chat set only (reads, Discord replies, approval requests)
  5. permanent deny lists (other-agent, personal companions; spec 11.9)
  6. taint: after untrusted input, R3 is blocked and R2 runs only if pre-declared;
     the Rule of Two per turn (spec 13.6)
  7. circuit breaker per adapter (spec 13.8)
  8. R3: a granted, unexpired, unused approval whose action hash equals this exact call
     and whose executor is the caller (spec 13.5). It is consumed only after every other
     check has passed, atomically, so a denied call never burns a token and two racing
     calls can never both use one.
  9. execute through the adapter; 10. record an immutable receipt (also for denials).

Everything is persisted in Postgres (mesh.receipts, mesh.approvals, mesh.turn_state), so a
restart loses nothing and every Toolgate process sees the same tokens. Zero LLM.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from ..deck.board import next_id
from ..deck.events import append_event

TIER_ORDER = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}

# Authority matrix (spec 7.4). R4 is never callable by agents; the Boss acts through the CLI/Console.
DEFAULT_AUTHORITY = {
    "logan": "R3",          # the Boss's own tool calls still pass the gate; R4 is not a tool
    "duke": "R2",           # R3 = request only
    "hermes": "R2",         # R3 = request only; build tasks only when the Boss assigns (D-003)
    "frank": "R3",          # executes R3 with a token
    "svc-verifier": "R1",   # R1 = "sandbox runs" (7.4): it re-runs checks, and nothing else
    "svc-reader": "R0",
    "svc-scribe": "R1",     # "R1: Scribe only"
    "svc-tooltester": "R1", # spec 12.7.8: "Never call tools above R1"
    "cofounder": "R0",        # G-4 default: ask-only
}
R3_EXECUTORS = {"frank", "logan"}

# Tool catalog (spec 13.3): tool -> (tier, taint_source)
TOOL_CATALOG: dict[str, tuple[str, bool]] = {
    "board.task_get": ("R0", False), "board.task_list": ("R0", False),
    "board.decision_get": ("R0", False), "board.knowledge_page_get": ("R0", False),
    "board.task_propose": ("R2", False), "board.task_claim": ("R2", False),
    "board.task_update": ("R2", False), "board.ledger_update": ("R1", False),
    "board.dependency_add": ("R2", False), "board.submit_completion": ("R2", False),
    "comms.send": ("R2", False), "comms.reply_discord": ("R2", False),
    "comms.huddle_convene": ("R2", False), "comms.escalate": ("R2", False),
    "comms.external_send": ("R3", False),
    "brain.recall": ("R0", False), "brain.reflect": ("R0", False),
    "brain.beliefs": ("R0", False), "brain.disagreements": ("R0", False),
    "brain.remember_private": ("R1", False), "brain.propose_memory": ("R2", False),
    "artifacts.get": ("R0", False), "artifacts.put": ("R1", False), "artifacts.link": ("R2", False),
    "skills.list": ("R0", False), "skills.load": ("R0", False), "skills.propose": ("R2", False),
    "repo.read": ("R0", False), "repo.grep": ("R0", False), "repo.diff": ("R0", False), "repo.log": ("R0", False),
    "repo.worktree_open": ("R1", False), "repo.edit": ("R1", False),
    "repo.run": ("R1", False), "repo.commit": ("R1", False),
    "repo.push_branch": ("R2", False), "repo.open_pr": ("R2", False), "repo.comment_pr": ("R2", False),
    "repo.merge_main": ("R3", False),
    "check.run": ("R1", False),  # runs a (non-author approved) contract check in the task worktree  # runs a contract acceptance check; the verifier's tool (spec 9.11)
    "deploy.staging_deploy": ("R2", False), "deploy.preview_url": ("R2", False),
    "deploy.prod_deploy": ("R3", False), "deploy.rollback": ("R3", False),  # R2 only via pre-approved incident runbooks (G-9)
    "ops.health": ("R0", False), "ops.logs_query": ("R0", False), "ops.metrics_query": ("R0", False),
    "ops.restart_service": ("R2", False), "ops.backup_run": ("R1", False),
    "ops.restore": ("R3", False), "ops.secret_rotate": ("R3", False),
    "ops.permission_change": ("R3", False), "ops.dns_change": ("R3", False),
    "web.search": ("R0", True), "web.fetch": ("R0", True),
    "crm.prospects_list": ("R0", False), "crm.prospect_get": ("R0", False), "crm.prospect_update": ("R2", False),
    "money.price_quote_calc": ("R0", False), "money.margin_calc": ("R0", False),
    "money.format_currency": ("R0", False), "money.quote_draft": ("R1", False),
    "money.invoice_draft": ("R1", False), "money.quote_send": ("R3", False),
    "money.invoice_send": ("R3", False), "money.purchase": ("R3", False),
    "sys.budget_status": ("R0", False), "sys.whoami": ("R0", False),
    "sys.policy_explain": ("R0", False), "sys.request_approval": ("R2", False),
    "sys.turn_done": ("R0", False),  # an agent runtime without a process exit (Hermes gateway) signals the end of its turn
}
# Tools that count toward the Rule of Two (spec 13.6): sensitive systems / external effects.
SENSITIVE = {"repo.edit", "repo.run", "repo.commit", "crm.prospect_get", "crm.prospects_list", "ops.logs_query",
             "ops.restore", "ops.secret_rotate", "ops.permission_change"}
EXTERNAL = {"comms.external_send", "repo.push_branch", "repo.open_pr", "repo.comment_pr", "repo.merge_main",
            "deploy.staging_deploy", "deploy.prod_deploy", "deploy.rollback", "money.quote_send", "money.invoice_send",
            "money.purchase", "ops.dns_change", "ops.restart_service"}

# A turn without a task can read, reply to the Boss, and ask for approval; nothing else.
CHAT_CAPABILITIES = {"board.read", "brain.recall", "comms.send", "sys.read", "sys.approval_request", "skills.read"}
# sys.read covers sys.whoami / sys.budget_status / sys.policy_explain / sys.turn_done (all R0)
# Capability "namespace.level" grants that namespace's tools up to the level's tier.
CAPABILITY_TIER = {"read": "R0", "ledger": "R1", "private": "R1", "worktree": "R1", "write": "R2", "propose": "R2",
                   "send": "R2", "branch": "R2", "staging": "R2", "restart": "R2", "backup": "R1", "share": "R2",
                   "approval_request": "R2", "calc": "R0", "draft": "R1", "external": "R3", "merge_main": "R3",
                   "prod": "R3", "restore": "R3", "secrets": "R3", "dns": "R3", "run": "R1"}

BANK_DENY_LIST = {"private-agent-3", "private-agent-2", "private-agent", "other-agent"}
BANK_DENY_PREFIXES = ("other-agent", "private/", "archived-bank")
PATH_DENY_PREFIXES = ("/home/user/other-agent", os.path.expanduser("~/other-agent"), "~/other-agent", "other-agent/")

TURN_TTL = timedelta(hours=2)
CIRCUIT_WINDOW_S = 300
CIRCUIT_FAILS = 3


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def action_hash(tool: str, args: dict[str, Any]) -> bytes:
    """spec 13.1: action_hash = sha256(tool ‖ canonical_json(args))."""
    return hashlib.sha256(tool.encode() + b"\x00" + canonical_json(args).encode()).digest()


def capability_covers(caps: set[str], tool: str) -> bool:
    ns = tool.split(".", 1)[0]
    tier = TIER_ORDER[TOOL_CATALOG[tool][0]]
    for c in caps:
        cns, _, level = c.partition(".")
        if cns != ns:
            continue
        if level == "*" or TIER_ORDER.get(CAPABILITY_TIER.get(level, "R0"), 0) >= tier:
            return True
    return False


@dataclass
class GateResult:
    allowed: bool
    reason: str
    receipt_id: str | None = None
    result: Any = None
    code: str = "ok"


# kept for the phase 3 host scaffolding (agents/crew.py)
@dataclass
class SessionToken:
    agent: str
    task_id: str | None
    turn_id: str
    capabilities: list[str]
    taint: bool = False
    planned_actions: list[str] = field(default_factory=list)


Adapter = Callable[[dict[str, Any], dict[str, Any]], tuple[Any, str, int]]
"""adapter(args, ctx) -> (result, deterministic summary, exit_code). ctx: agent, task_id, turn_id."""


class Toolgate:
    def __init__(self, dsn: str | None = None, adapters: dict[str, Adapter] | None = None):
        self.dsn = dsn or os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")
        self.adapters: dict[str, Adapter] = dict(adapters or {})

    # ── turns ────────────────────────────────────────────────────────────────
    def open_turn(self, agent: str, task_id: str | None = None, ttl: timedelta = TURN_TTL) -> str:
        """Start a gate session for one agent turn. Capabilities come from the task's contract
        on the board, never from the caller."""
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT kind FROM mesh.principals WHERE id=%s AND active", (agent,))
            if not cur.fetchone():
                raise PermissionError(f"unknown principal {agent!r}")
            if task_id:
                cur.execute("SELECT state, lease_holder, lease_expires_at FROM mesh.tasks WHERE id=%s", (task_id,))
                row = cur.fetchone()
                if not row:
                    raise LookupError(f"task {task_id} does not exist")
                state, holder, exp = row
                live = holder and exp and exp > datetime.now(timezone.utc)
                holder_base = (holder or "").split(":", 1)[0]
                if agent == "svc-verifier":
                    if state != "IN_REVIEW":
                        raise PermissionError(f"the verifier opens turns only on IN_REVIEW tasks ({task_id} is {state})")
                elif not (state == "WORKING" and live and holder_base == agent):
                    raise PermissionError(f"{agent} does not hold a live lease on {task_id} ({state}, holder {holder}); "
                                          "a task's capabilities belong to its lease holder only")
            tid = next_id(cur, "TU")
            cur.execute("INSERT INTO mesh.turn_state (turn_id, agent, task_id, expires_at) VALUES (%s,%s,%s,%s)",
                        (tid, agent, task_id, datetime.now(timezone.utc) + ttl))
        return tid

    def declare_plan(self, turn_id: str, agent: str, actions: list[str]) -> None:
        """Plan-then-execute (spec 13.6): R2 actions allowed after an untrusted read must be
        registered BEFORE the first tainted read. After that the plan is locked."""
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT agent, planned_locked FROM mesh.turn_state WHERE turn_id=%s FOR UPDATE", (turn_id,))
            row = cur.fetchone()
            if not row or row[0] != agent:
                raise PermissionError("not your turn")
            if row[1]:
                raise PermissionError("plan is locked: this turn already read untrusted content")
            unknown = [a for a in actions if a not in TOOL_CATALOG]
            if unknown:
                raise ValueError(f"unknown tools in plan: {unknown}")
            cur.execute("UPDATE mesh.turn_state SET planned_actions=%s WHERE turn_id=%s", (sorted(set(actions)), turn_id))

    def _caps(self, cur, task_id: str | None, agent: str) -> set[str]:
        """Capabilities are re-derived on every call from the board: the contract's list while the
        caller holds a live lease on a WORKING task; only check.run for the verifier on an
        IN_REVIEW task; the chat set otherwise (the lease expired, the task moved on)."""
        if not task_id:
            return set(CHAT_CAPABILITIES)
        cur.execute("SELECT contract->'capabilities', state, lease_holder, lease_expires_at FROM mesh.tasks WHERE id=%s", (task_id,))
        row = cur.fetchone()
        if not row:
            return set(CHAT_CAPABILITIES)
        caps, state, holder, exp = row
        if agent == "svc-verifier":
            return {"check.run"} if state == "IN_REVIEW" else set()
        if state == "WORKING" and (holder or "").split(":", 1)[0] == agent and exp and exp > datetime.now(timezone.utc):
            return set(caps or []) | {"board.read", "board.ledger", "comms.send", "sys.read", "sys.approval_request", "brain.recall", "check.run"}
        return set(CHAT_CAPABILITIES)

    # ── the gate ─────────────────────────────────────────────────────────────
    def call(self, turn_id: str, agent: str, tool: str, args: dict[str, Any], *,
             approval_id: str | None = None, idempotency_key: str | None = None) -> GateResult:
        """Three transactions, so nothing that matters depends on a commit that happens after
        the effect: (A) authorize, reserve the idempotency key, spend the R3 token -> COMMIT;
        (B) run the adapter with no transaction or lock held; (C) write the receipt and the
        turn's taint state. A crash between B and C leaves a spent token and a reserved key:
        the effect can never run twice (at most once), and the gap is visible as a key with
        no receipt."""
        t0 = datetime.now(timezone.utc)
        args = args or {}
        digest = hashlib.sha256(canonical_json({"tool": tool, "args": args}).encode()).digest()
        tier_s, taint_source = TOOL_CATALOG.get(tool, ("R3", False))

        def write_receipt(cur, ts, status: str, *, reason=None, result=None, summary=None, exit_code=None,
                          taint_in=False, appr=None, check_id=None) -> str:
            rid = next_id(cur, "RC")
            safe = json.loads(json.dumps(result, default=str)) if result is not None else None
            cur.execute(
                """INSERT INTO mesh.receipts (id, turn_id, task_id, agent, tool, risk_tier, args_digest, result_digest,
                   status, denial_reason, exit_code, summary, approval_id, taint_in, idempotency_key, started_at, ended_at,
                   result, check_id) VALUES (%s, NULL, %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), %s, %s)""",
                (rid, ts["task_id"] if ts else None, agent, tool, tier_s, digest,
                 hashlib.sha256(canonical_json(safe).encode()).digest() if safe is not None else None,
                 status, reason, exit_code, summary, appr, taint_in,
                 idempotency_key if status == "ok" else None, t0, Jsonb(safe) if safe is not None else None, check_id),
            )
            append_event(cur, agent, "tool.denied" if status == "denied" else "tool.called",
                         {"receipt_id": rid, "tool": tool, "status": status, "reason": reason, "turn_id": turn_id},
                         "receipt", rid, correlation_id=ts["task_id"] if ts else None)
            return rid

        # ── A: authorize ─────────────────────────────────────────────────────
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM mesh.principals WHERE id=%s AND active", (agent,))
            if not cur.fetchone():
                raise PermissionError(f"unknown principal {agent!r}")  # unauthenticated: no receipt to attach
            ts = None

            def deny(reason: str, code: str) -> GateResult:
                rid = write_receipt(cur, ts, "denied", reason=reason, taint_in=bool(ts and ts["tainted"]))
                return GateResult(False, reason, rid, code=code)

            # 1. identity
            cur.execute("SELECT turn_id, agent, task_id, tainted, planned_actions, sensitive, external, expires_at "
                        "FROM mesh.turn_state WHERE turn_id=%s FOR UPDATE", (turn_id,))
            row = cur.fetchone()
            if not row:
                return deny(f"unknown turn {turn_id}", "unknown_turn")
            if row[1] != agent:
                return deny(f"turn {turn_id} belongs to {row[1]}", "not_your_turn")
            ts = dict(zip(["turn_id", "agent", "task_id", "tainted", "planned", "sensitive", "external", "expires_at"], row))
            if ts["expires_at"] < t0:
                return deny("turn expired; open a new turn", "turn_expired")

            # 0. idempotency (after identity; bound to agent + tool + exact args; reserved atomically)
            if idempotency_key:
                cur.execute("INSERT INTO mesh.idempotency (key, agent, tool, args_digest) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (key) DO NOTHING RETURNING key", (idempotency_key, agent, tool, digest))
                if not cur.fetchone():
                    cur.execute("SELECT agent, tool, args_digest, receipt_id FROM mesh.idempotency WHERE key=%s", (idempotency_key,))
                    k_agent, k_tool, k_digest, k_receipt = cur.fetchone()
                    if k_agent != agent or k_tool != tool or bytes(k_digest) != digest:
                        return deny("idempotency key already used for a different caller or action", "idempotency_mismatch")
                    if not k_receipt:
                        return deny("a call with this idempotency key is already in flight (or crashed before its receipt)", "idempotency_in_flight")
                    cur.execute("SELECT result FROM mesh.receipts WHERE id=%s", (k_receipt,))
                    return GateResult(True, "idempotent-replay", k_receipt, cur.fetchone()[0], code="replay")

            # 2. catalog
            if tool not in TOOL_CATALOG:
                return deny(f"unknown tool {tool!r}", "unknown_tool")
            tier = TIER_ORDER[tier_s]

            # 3. authority
            if tier > TIER_ORDER[DEFAULT_AUTHORITY.get(agent, "R0")]:
                if tier == 3:
                    return deny(f"{tool} is R3: {agent} may only request it (sys.request_approval); Frank executes with the Boss's token",
                                "r3_request_only")
                return deny(f"{tool} is {tier_s}, above {agent}'s maximum {DEFAULT_AUTHORITY.get(agent, 'R0')}", "tier_exceeded")

            # 4. least privilege
            caps = self._caps(cur, ts["task_id"], agent)
            if not capability_covers(caps, tool):
                return deny(f"{tool} is not covered by this turn's capabilities {sorted(caps)}", "capability_missing")

            # 5. deny lists
            for k, v in args.items():
                if isinstance(v, str):
                    low = v.lower()
                    if k in ("bank", "bank_id") and (low in BANK_DENY_LIST or low.startswith(BANK_DENY_PREFIXES)):
                        return deny(f"bank {v!r} is permanently denied to the crew", "deny_list")
                    if k in ("path", "repo", "cwd") and low.startswith(PATH_DENY_PREFIXES):
                        return deny(f"path {v!r} is outside the Blimp estate (other-agent)", "deny_list")

            # 6. taint + Rule of Two
            if ts["tainted"]:
                if tier >= 3:
                    return deny("R3 is blocked in a turn that read untrusted content; end the turn and request approval from a clean turn", "tainted_r3")
                if tier >= 1 and tool not in (ts["planned"] or []) and tool in SENSITIVE | EXTERNAL | {"check.run"}:
                    return deny("after an untrusted read, this action needs to be pre-declared (plan-then-execute)", "tainted_r2")
                if tier == 2 and tool not in (ts["planned"] or []):
                    return deny("R2 after an untrusted read needs the action pre-declared (plan-then-execute)", "tainted_r2")
            would = {"untrusted": ts["tainted"] or taint_source, "sensitive": ts["sensitive"] or tool in SENSITIVE,
                     "external": ts["external"] or tool in EXTERNAL}
            if sum(would.values()) > 2:
                return deny("Rule of Two: this turn already combines two of untrusted input / sensitive data / external effect", "rule_of_two")

            # 7. circuit breaker (adapter outages only: a failing check is a result, a refused input
            #    (exit 3) is the caller's mistake, an unwired tool (127) is not an outage)
            ns = tool.split(".", 1)[0]
            if ns != "check":
                cur.execute("SELECT status FROM mesh.receipts WHERE tool LIKE %s AND status IN ('ok','error') "
                            "AND COALESCE(exit_code, 1) NOT IN (3, 127) AND started_at > now() - make_interval(secs => %s) "
                            "ORDER BY started_at DESC LIMIT %s", (ns + ".%", CIRCUIT_WINDOW_S, CIRCUIT_FAILS))
                recent = [r[0] for r in cur.fetchall()]
                if len(recent) == CIRCUIT_FAILS and all(s == "error" for s in recent):
                    return deny(f"circuit open for {ns} ({CIRCUIT_FAILS} consecutive failures); retry in 5 minutes", "circuit_open")

            adapter = self.adapters.get(tool)
            # 8. R3 approval: validated, then spent and COMMITTED before anything executes
            appr = None
            if tier == 3:
                if not approval_id:
                    return deny("AUTH_REQUIRED: this R3 action needs the Boss's approval token (sys.request_approval)", "auth_required")
                cur.execute("SELECT status, action_hash, executor, expires_at, consumed_at FROM mesh.approvals WHERE id=%s FOR UPDATE",
                            (approval_id,))
                a = cur.fetchone()
                if not a:
                    return deny(f"approval {approval_id} not found", "approval_missing")
                status, ahash, executor, expires, consumed = a
                if status == "consumed" or consumed:
                    return deny(f"approval {approval_id} already used (single-use)", "approval_used")
                if status != "granted":
                    return deny(f"approval {approval_id} is {status}, not granted", "approval_not_granted")
                if expires < t0:
                    cur.execute("UPDATE mesh.approvals SET status='expired' WHERE id=%s", (approval_id,))
                    return deny(f"approval {approval_id} expired", "approval_expired")
                if bytes(ahash) != action_hash(tool, args):
                    return deny("approval does not match this exact action (tool + args)", "approval_mismatch")
                if executor != agent:
                    return deny(f"approval names {executor} as executor, not {agent}", "wrong_executor")
                if adapter is None:
                    return deny(f"{tool} has no adapter wired yet; the approval stays unused", "not_wired")
                cur.execute("UPDATE mesh.approvals SET status='consumed', consumed_at=now() WHERE id=%s AND status='granted'", (approval_id,))
                append_event(cur, agent, "approval.consumed", {"approval_id": approval_id, "tool": tool}, "approval", approval_id)
                appr = approval_id
            if adapter is None:
                rid = write_receipt(cur, ts, "error", reason="no adapter wired for this tool yet", exit_code=127)
                if idempotency_key:
                    cur.execute("DELETE FROM mesh.idempotency WHERE key=%s", (idempotency_key,))
                return GateResult(False, f"{tool} has no adapter wired yet (it cannot run, and nothing pretends it did)", rid, code="not_wired")
            task_id = ts["task_id"]
        # COMMIT of A happened on leaving the block: token spent, key reserved, no locks held.

        # ── B: execute, outside any transaction ─────────────────────────────
        try:
            result, summary, exit_code = adapter(args, {"agent": agent, "task_id": task_id, "turn_id": turn_id})
            status = "ok" if exit_code == 0 else "error"
        except (PermissionError, ValueError, LookupError, TypeError, KeyError) as e:
            # the input was refused (outside the worktree, bad argument): the caller's mistake
            result, summary, exit_code, status = None, f"refused: {type(e).__name__}: {str(e)[:300]}", 3, "error"
        except Exception as e:
            result, summary, exit_code, status = None, f"{type(e).__name__}: {str(e)[:300]}", 1, "error"

        # ── C: receipt + turn state ──────────────────────────────────────────
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT tainted FROM mesh.turn_state WHERE turn_id=%s FOR UPDATE", (turn_id,))
            was_tainted = cur.fetchone()[0]
            cur.execute("UPDATE mesh.turn_state SET tainted = tainted OR %s, planned_locked = planned_locked OR %s, "
                        "taint_sources = CASE WHEN %s THEN array_append(taint_sources, %s) ELSE taint_sources END, "
                        "sensitive = sensitive OR %s, external = external OR %s WHERE turn_id=%s",
                        (taint_source, taint_source, taint_source, tool, tool in SENSITIVE, tool in EXTERNAL, turn_id))
            rid = write_receipt(cur, {"task_id": task_id}, status, result=result, summary=summary, exit_code=exit_code,
                                taint_in=bool(was_tainted or taint_source), appr=appr,
                                check_id=args.get("check_id") if tool == "check.run" else None)
            if idempotency_key:
                cur.execute("UPDATE mesh.idempotency SET receipt_id=%s WHERE key=%s", (rid, idempotency_key))
            if taint_source and not was_tainted:
                append_event(cur, agent, "taint.raised", {"turn_id": turn_id, "source": tool}, "turn", turn_id)
        return GateResult(status == "ok", "ok" if status == "ok" else (summary or "error"), rid, result, code=status)

    # ── approvals (spec 13.5) ───────────────────────────────────────────────
    def request_approval(self, *, requested_by: str, executor: str, tool: str, args: dict[str, Any], why: str,
                         rollback: str | None, evidence: list[str] | None = None, task_id: str | None = None,
                         cost_estimate_usd: float | None = None, ttl: timedelta = timedelta(hours=24),
                         urgent: bool = False) -> dict[str, Any]:
        """urgent=True skips the decision-window batching (sev1/sev2, or the Boss waiting on it)."""
        if tool not in TOOL_CATALOG or TOOL_CATALOG[tool][0] != "R3":
            raise ValueError(f"{tool} is not an R3 tool; approvals are only for R3")
        if executor not in R3_EXECUTORS:
            raise ValueError(f"executor must be one of {sorted(R3_EXECUTORS)}")
        if not (why or "").strip() or not (rollback or "").strip():
            raise ValueError("an approval card needs why and rollback")
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            aid = next_id(cur, "AP")
            exp = datetime.now(timezone.utc) + ttl
            cur.execute(
                """INSERT INTO mesh.approvals (id, task_id, requested_by, executor, tool, args, action_hash, why, rollback, evidence,
                   cost_estimate_usd, status, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)""",
                (aid, task_id, requested_by, executor, tool, Jsonb(args), action_hash(tool, args), why, rollback, evidence or [],
                 cost_estimate_usd, exp),
            )
            append_event(cur, requested_by, "approval.requested",
                         {"approval_id": aid, "tool": tool, "args": args, "executor": executor, "why": why, "rollback": rollback,
                          "evidence": evidence or [], "task_id": task_id, "urgent": urgent,
                          "cost_estimate_usd": cost_estimate_usd}, "approval", aid, correlation_id=task_id)
        return {"id": aid, "status": "pending", "expires_at": exp, "action_hash": action_hash(tool, args).hex()}

    def decide_approval(self, approval_id: str, decided_by: str, grant: bool, note: str | None = None) -> dict[str, Any]:
        """Only the Boss decides (the Bridge verifies his Discord user id before calling this)."""
        if decided_by != "logan":
            raise PermissionError("only the Boss can grant or deny an R3 approval")
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT status, expires_at, requested_by FROM mesh.approvals WHERE id=%s FOR UPDATE", (approval_id,))
            row = cur.fetchone()
            if not row:
                raise LookupError(f"approval {approval_id} not found")
            if row[0] != "pending":
                raise ValueError(f"approval {approval_id} is already {row[0]}")
            if row[1] < datetime.now(timezone.utc):
                cur.execute("UPDATE mesh.approvals SET status='expired' WHERE id=%s", (approval_id,))
                conn.commit()
                raise ValueError(f"approval {approval_id} expired")
            status = "granted" if grant else "denied"
            cur.execute("UPDATE mesh.approvals SET status=%s, decided_by='logan', decided_at=now(), decision_note=%s WHERE id=%s",
                        (status, note, approval_id))
            append_event(cur, "logan", f"approval.{status}", {"approval_id": approval_id, "requested_by": row[2], "note": note},
                         "approval", approval_id)
        return {"id": approval_id, "status": status}

    def expire_approvals(self) -> list[str]:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute("UPDATE mesh.approvals SET status='expired' WHERE status IN ('pending','granted') AND expires_at < now() RETURNING id")
            ids = [r[0] for r in cur.fetchall()]
            for aid in ids:
                append_event(cur, "frank", "approval.expired", {"approval_id": aid}, "approval", aid)
        return ids
