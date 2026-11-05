"""Blimp Crew v2 — Deck board (spec §9, §8.4, §15): tasks, contracts, leases, ledgers,
threads, messages, mandates and decisions.

Zero LLM. Every state change and its event commit in ONE transaction (spec §10.1), so the
board and the event log can never disagree. The board is the source of truth (spec §8.1
L-State): agents read it, messages point at it.

Rules enforced here, in code:
- The task lifecycle is the state machine in spec §9.4; any other transition is refused.
- A task cannot become READY until its contract passes the lint (§9.3) AND its owner
  ACCEPTs; the contract freezes at READY (§14.2).
- Every task traces to an objective or mandate, or is maintenance/incident (§9.1).
- Leases are single-holder with a TTL; only the lease holder writes the ledger (§10.5).
- progress.is_looping is Ballast's field; an agent cannot set it (§9.8).
- Messages must reference an existing task whose context matches (§8.3), pass envelope
  validation, and carry one intent (warn, then reject; §8.11).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .contract_lint import lint_contract
from .envelope import VALID_ACTS, validate_envelope
from .events import append_event

SPEECH_ACT_EXT = "https://blimp.my/a2a/ext/speech-act/v1"
DEFAULT_LEASE = timedelta(minutes=30)

STATES = {
    "DRAFT", "PROPOSED", "READY", "WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED", "IN_REVIEW",
    "BLOCKED", "PARKED", "DONE", "FAILED", "CANCELED", "REJECTED",
}
TERMINAL = {"DONE", "FAILED", "CANCELED", "REJECTED"}
PLAN_STATUSES = {"todo", "doing", "done", "blocked", "skipped"}
# v1 agents have no host renewing leases; a claim through the tools lasts the contract's
# wall_clock_h (spec 9.4: "extended to the task's wall_clock_h between turns"), default 4h.
DEFAULT_TOOL_LEASE = timedelta(hours=4)

# spec §9.4 stateDiagram, edge for edge. READY and IN_REVIEW/DONE have dedicated entry
# points (make_ready / claim / submit_for_review / verification) that enforce their gates.
TRANSITIONS: dict[str, set[str]] = {
    "DRAFT": {"PROPOSED"},
    "PROPOSED": {"READY", "REJECTED"},
    "READY": {"WORKING"},
    "WORKING": {"INPUT_REQUIRED", "AUTH_REQUIRED", "IN_REVIEW", "BLOCKED", "PARKED", "FAILED", "CANCELED"},
    "INPUT_REQUIRED": {"WORKING"},
    "AUTH_REQUIRED": {"WORKING"},
    "IN_REVIEW": {"DONE", "WORKING"},
    "BLOCKED": {"READY"},
    "PARKED": {"READY"},
}
GATED = {("PROPOSED", "READY"), ("READY", "WORKING"), ("WORKING", "IN_REVIEW"), ("IN_REVIEW", "DONE")}

# spec §9.4 "Mapping to A2A TaskState"
A2A_STATE = {
    "DRAFT": "SUBMITTED", "PROPOSED": "SUBMITTED", "READY": "SUBMITTED",
    "WORKING": "WORKING", "IN_REVIEW": "WORKING", "BLOCKED": "WORKING", "PARKED": "WORKING",
    "INPUT_REQUIRED": "INPUT_REQUIRED", "AUTH_REQUIRED": "AUTH_REQUIRED",
    "DONE": "COMPLETED", "FAILED": "FAILED", "CANCELED": "CANCELED", "REJECTED": "REJECTED",
}

TASK_TYPES = {"design", "research", "build", "fix", "ops_change", "incident", "report", "client_delivery",
              "sales_support", "maintenance", "learning", "initiative"}
UNTRACED_OK = {"maintenance", "incident"}
DEFAULT_OWNER = {"design": "hermes", "research": "hermes", "build": "duke", "fix": "duke", "ops_change": "frank",
                 "incident": "frank", "report": "frank", "initiative": "hermes"}

# One intent per message (§8.11): a second line that starts with another act keyword.
_ACT_LINE = re.compile(r"^\s*(" + "|".join(sorted(VALID_ACTS)) + r")\s*:", re.M)


class BoardError(Exception):
    """A refused board operation. `code` is stable for callers (A2A 400-class errors)."""

    def __init__(self, code: str, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or []

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "details": self.details}


@dataclass
class Principal:
    id: str
    kind: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def contract_sha(contract: dict) -> str:
    return hashlib.sha256(json.dumps(contract, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def base_principal(agent: str) -> str:
    """'duke:pairing' (an interactive session holding Duke's lease, spec 10.4) acts as 'duke'."""
    return agent.split(":", 1)[0]


def next_id(cur: psycopg.Cursor, prefix: str, width: int = 4) -> str:
    """Issue the next human-readable id for a prefix (T-0001, D-011, ...). Atomic."""
    cur.execute(
        """INSERT INTO mesh.id_counters (prefix, last) VALUES (%s, 1)
           ON CONFLICT (prefix) DO UPDATE SET last = mesh.id_counters.last + 1
           RETURNING last""",
        (prefix,),
    )
    n = cur.fetchone()[0]
    return f"{prefix}-{n:0{width}d}"


class Board:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")

    # ── plumbing ────────────────────────────────────────────────────────────
    def _tx(self):
        return psycopg.connect(self.dsn)

    @contextlib.contextmanager
    def _cur(self, cur=None):
        """Use the caller's cursor (one transaction across several board operations) or open one."""
        if cur is not None:
            yield cur
            return
        with self._tx() as conn, conn.cursor() as c:
            yield c

    def _principal(self, cur, pid: str) -> Principal:
        cur.execute("SELECT id, kind FROM mesh.principals WHERE id=%s AND active", (pid,))
        row = cur.fetchone()
        if not row:
            raise BoardError("unknown_principal", f"'{pid}' is not an active principal")
        return Principal(*row)

    def _task_row(self, cur, task_id: str, lock: bool = False) -> dict[str, Any]:
        cur.execute(
            "SELECT id, title, type, state, owner, reviewer, context_id, contract, contract_frozen_at, "
            "lease_holder, lease_expires_at, risk_tier, effort_class, budget, mandate_id, objective_id, "
            "created_by, version, pinned, priority, client_id, due_at, substate FROM mesh.tasks WHERE id=%s" + (" FOR UPDATE" if lock else ""),
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            raise BoardError("task_not_found", f"task {task_id} does not exist")
        keys = ["id", "title", "type", "state", "owner", "reviewer", "context_id", "contract", "contract_frozen_at",
                "lease_holder", "lease_expires_at", "risk_tier", "effort_class", "budget", "mandate_id",
                "objective_id", "created_by", "version", "pinned", "priority", "client_id", "due_at", "substate"]
        return dict(zip(keys, row))

    def _set_state(self, cur, task: dict, to: str, actor: str, reason: str | None, **extra) -> None:
        frm = task["state"]
        cur.execute(
            "UPDATE mesh.tasks SET state=%s, substate=%s, updated_at=now(), version=version+1 WHERE id=%s",
            (to, extra.get("substate"), task["id"]),
        )
        append_event(cur, actor, "task.state_changed",
                     {"from": frm, "to": to, "reason": reason, "a2a_state": A2A_STATE[to], **{k: v for k, v in extra.items() if v is not None}},
                     "task", task["id"], correlation_id=task["id"])
        task["state"] = to

    # ── mandates and decisions (C5: verbatim, append-only) ───────────────────
    def record_mandate(self, verbatim: str, source: str, tags: list[str] | None = None, recorded_by: str = "logan") -> str:
        """Store the Boss's words exactly as written. Never paraphrased, never edited.

        recorded_by: who captured them (the Discord Bridge captures automatically in phase 3;
        until then an agent copies them in). It is stored as a tag and on the event, so a
        mandate an agent typed is always distinguishable from one the Bridge captured."""
        if not verbatim or not verbatim.strip():
            raise BoardError("empty_mandate", "a mandate needs the Boss's words")
        with self._tx() as conn, conn.cursor() as cur:
            mid = next_id(cur, "M")
            cur.execute(
                "INSERT INTO mesh.mandates (id, received_at, source, verbatim, sha256, tags) VALUES (%s, now(), %s, %s, %s, %s)",
                (mid, source, verbatim, hashlib.sha256(verbatim.encode()).digest(), [*(tags or []), f"recorded_by:{recorded_by}"]),
            )
            self._principal(cur, recorded_by)
            append_event(cur, recorded_by, "mandate.received",
                         {"mandate_id": mid, "source": source, "chars": len(verbatim), "recorded_by": recorded_by}, "mandate", mid)
        return mid

    def record_decision(self, *, title: str, context: str, decision: str, deciders: list[str], actor: str,
                        alternatives: list | None = None, consequences: str | None = None, dissent: str | None = None,
                        logan_verbatim: str | None = None, status: str = "accepted", supersedes: str | None = None,
                        task_ids: list[str] | None = None, huddle_id: str | None = None) -> str:
        """Decision record (spec §16.6.2). logan_verbatim is required when the Boss decided."""
        if "logan" in deciders and not (logan_verbatim or "").strip():
            raise BoardError("verbatim_required", "the Boss decided: logan_verbatim is required (C5)")
        if status not in ("proposed", "accepted", "superseded", "rejected"):
            raise BoardError("bad_status", f"decision status {status!r}")
        with self._tx() as conn, conn.cursor() as cur:
            did = next_id(cur, "D", width=3)
            cur.execute(
                """INSERT INTO mesh.decisions (id, title, status, context, decision, alternatives, consequences, dissent,
                   deciders, logan_verbatim, supersedes, huddle_id, task_ids) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (did, title, status, context, decision, Jsonb(alternatives or []), consequences, dissent, deciders,
                 logan_verbatim, supersedes, huddle_id, task_ids or []),
            )
            if supersedes:
                cur.execute("UPDATE mesh.decisions SET status='superseded' WHERE id=%s", (supersedes,))
                append_event(cur, actor, "decision.superseded", {"decision_id": supersedes, "by": did}, "decision", supersedes)
            append_event(cur, actor, "decision.accepted" if status == "accepted" else "decision.proposed",
                         {"decision_id": did, "title": title}, "decision", did)
        return did

    # ── task lifecycle ───────────────────────────────────────────────────────
    def create_task(self, *, title: str, type: str, created_by: str, owner: str | None = None,
                    mandate_id: str | None = None, objective_id: str | None = None, parent_id: str | None = None,
                    risk_tier: str = "R1", effort_class: str = "S", budget: dict | None = None,
                    client_id: str | None = None, contract: dict | None = None, cur=None) -> dict[str, Any]:
        """New task in DRAFT, with its thread (context) created in the same transaction."""
        if type not in TASK_TYPES:
            raise BoardError("bad_type", f"task type {type!r} not in {sorted(TASK_TYPES)}")
        if not title.strip() or len(title) > 90:
            raise BoardError("bad_title", "title must be 1-90 characters")
        if type not in UNTRACED_OK and not (mandate_id or objective_id):
            raise BoardError("untraceable", "every task traces to a mandate or objective, or is maintenance/incident (spec 9.1)")
        with self._cur(cur) as cur:
            creator = self._principal(cur, created_by)
            # maintenance/learning/sales/client work has no fixed owner (spec 9.2 "Any"): the agent
            # who created it owns it; the Boss-created ones wait for an owner to be set.
            owner = owner or DEFAULT_OWNER.get(type) or (created_by if creator.kind == "agent" else None)
            if owner:
                self._principal(cur, owner)
            tid = next_id(cur, "T")
            ctx = f"ctx-{tid}"
            body = dict(contract or {})
            body.setdefault("id", tid)
            if body["id"] != tid:
                raise BoardError("id_mismatch", f"contract id {body['id']} != {tid}")
            cur.execute(
                """INSERT INTO mesh.tasks (id, title, type, state, owner, context_id, risk_tier, effort_class, contract,
                   budget, created_by, mandate_id, objective_id, parent_id, client_id)
                   VALUES (%s,%s,%s,'DRAFT',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (tid, title, type, owner, ctx, risk_tier, effort_class, Jsonb(body), Jsonb(budget or {"tokens": 0, "turns": 0}),
                 created_by, mandate_id, objective_id, parent_id, client_id),
            )
            cur.execute("INSERT INTO mesh.threads (context_id, kind, task_id) VALUES (%s, 'task', %s)", (ctx, tid))
            append_event(cur, created_by, "task.drafted", {"task_id": tid, "title": title, "type": type, "owner": owner},
                         "task", tid, correlation_id=tid)
        return {"id": tid, "context_id": ctx, "state": "DRAFT", "owner": owner}

    def propose(self, task_id: str, contract: dict, actor: str, cur=None) -> dict[str, Any]:
        """Attach the drafted contract; DRAFT -> PROPOSED. Lint runs now so problems surface early."""
        with self._cur(cur) as cur:
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] not in ("DRAFT", "PROPOSED"):
                raise BoardError("bad_transition", f"{task_id} is {t['state']}; contracts change only in DRAFT/PROPOSED (frozen at READY)")
            if actor not in (t["created_by"], t["owner"], "logan", "hermes"):
                # hermes drafts contracts for the crew (spec 7.3 RACI: task contract drafting R/A)
                raise BoardError("not_allowed", f"only {t['created_by']}, {t['owner']}, Hermes or the Boss may draft the contract of {task_id}")
            if contract.get("owner") and t["owner"] and contract["owner"] != t["owner"] and actor != "logan":
                raise BoardError("owner_change", f"{task_id} is owned by {t['owner']}; only the Boss reassigns ownership (use HANDOFF)")
            contract = {**contract, "id": task_id}
            for who in (contract.get("owner"), contract.get("reviewer")):
                if who:
                    self._principal(cur, who)
            ok, errors = self._lint(cur, contract)
            cur.execute("UPDATE mesh.tasks SET contract_author=%s, checks_approved_by=NULL, checks_approved_sha=NULL WHERE id=%s",
                        (base_principal(actor), task_id))
            cur.execute(
                "UPDATE mesh.tasks SET contract=%s, owner=COALESCE(%s, owner), reviewer=COALESCE(%s, reviewer), "
                "risk_tier=COALESCE(%s, risk_tier), effort_class=COALESCE(%s, effort_class), budget=COALESCE(%s, budget), "
                "updated_at=now(), version=version+1 WHERE id=%s",
                (Jsonb(contract), contract.get("owner"), contract.get("reviewer"), contract.get("risk_tier"),
                 contract.get("effort_class"), Jsonb(contract["budget"]) if contract.get("budget") else None, task_id),
            )
            if t["state"] == "DRAFT":
                self._set_state(cur, t, "PROPOSED", actor, "contract drafted")
            append_event(cur, actor, "task.proposed", {"task_id": task_id, "lint_ok": ok, "lint_errors": errors}, "task", task_id, correlation_id=task_id)
            if not ok:
                append_event(cur, "frank" if actor not in ("duke", "hermes", "frank") else actor, "task.lint_failed",
                             {"task_id": task_id, "errors": errors}, "task", task_id, correlation_id=task_id)
        return {"id": task_id, "state": "PROPOSED", "lint_ok": ok, "lint_errors": errors}

    def _lint(self, cur, contract: dict) -> tuple[bool, list[str]]:
        cur.execute("SELECT id, COALESCE(contract->'depends_on', '[]'::jsonb) FROM mesh.tasks")
        rows = cur.fetchall()
        ids = {r[0] for r in rows}
        edges = {r[0]: list(r[1] or []) for r in rows}
        return lint_contract(contract, existing_task_ids=ids, dependency_edges=edges)

    def make_ready(self, task_id: str, actor: str, reviewed_version: int | None = None) -> dict[str, Any]:
        """PROPOSED -> READY: the OWNER accepts, the lint passes, the contract freezes (spec 9.4, 14.2).

        reviewed_version: the task version the owner read. If the contract changed since, the
        accept is refused, so nobody freezes a contract they never saw."""
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] != "PROPOSED":
                raise BoardError("bad_transition", f"{task_id} is {t['state']}, not PROPOSED")
            if actor != t["owner"]:
                raise BoardError("not_owner", f"only the owner ({t['owner']}) can ACCEPT {task_id} into READY")
            if reviewed_version is not None and reviewed_version != t["version"]:
                raise BoardError("stale_version", f"{task_id} changed since you read it (you read v{reviewed_version}, now v{t['version']}); re-read and accept again")
            ok, errors = self._lint(cur, t["contract"])
            if not ok:
                append_event(cur, actor, "task.lint_failed", {"task_id": task_id, "errors": errors}, "task", task_id, correlation_id=task_id)
                conn.commit()
                raise BoardError("lint_failed", f"{task_id} contract fails lint", errors)
            if t["contract"].get("owner") and t["contract"]["owner"] != t["owner"]:
                raise BoardError("owner_mismatch", "contract owner differs from task owner")
            cur.execute("UPDATE mesh.tasks SET contract_frozen_at=now() WHERE id=%s", (task_id,))
            for dep in t["contract"].get("depends_on") or []:
                cur.execute("INSERT INTO mesh.task_edges (from_id, to_id, kind) VALUES (%s,%s,'blocks') ON CONFLICT DO NOTHING", (dep, task_id))
            self._set_state(cur, t, "READY", actor, "lint passed; owner accepted")
            append_event(cur, actor, "task.ready", {"task_id": task_id}, "task", task_id, correlation_id=task_id)
        return {"id": task_id, "state": "READY"}

    def approve_checks(self, task_id: str, approver: str) -> dict[str, Any]:
        """Separation of duties for runnable checks (phase 2 security review, P0): a test/cmd check runs
        a shell command, so it may only run after someone OTHER than the contract's author has read
        and approved this exact contract. Approval binds to the contract's hash; any later edit
        clears it."""
        with self._tx() as conn, conn.cursor() as cur:
            p = self._principal(cur, approver)
            if p.kind == "service":
                raise BoardError("not_allowed", "service agents do not approve checks")
            t = self._task_row(cur, task_id, lock=True)
            cur.execute("SELECT contract_author FROM mesh.tasks WHERE id=%s", (task_id,))
            author = cur.fetchone()[0]
            if not author:
                raise BoardError("no_contract", f"{task_id} has no proposed contract to approve")
            if approver == author:
                raise BoardError("self_approval", f"{approver} wrote this contract; someone else (the Boss or another agent) must approve its checks")
            if t["state"] in TERMINAL:
                raise BoardError("bad_transition", f"{task_id} is {t['state']}")
            sha = contract_sha(t["contract"])
            cur.execute("UPDATE mesh.tasks SET checks_approved_by=%s, checks_approved_sha=%s WHERE id=%s", (approver, sha, task_id))
            append_event(cur, approver, "task.checks_approved", {"task_id": task_id, "author": author, "contract_sha256": sha},
                         "task", task_id, correlation_id=task_id)
        return {"id": task_id, "checks_approved_by": approver, "contract_sha256": sha}

    def reject(self, task_id: str, actor: str, reason: str) -> dict[str, Any]:
        """PROPOSED -> REJECTED: the owner REJECTs, or the Boss kills it."""
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] != "PROPOSED":
                raise BoardError("bad_transition", f"{task_id} is {t['state']}, not PROPOSED")
            if actor not in (t["owner"], "logan"):
                raise BoardError("not_owner", "only the owner or the Boss can reject")
            self._set_state(cur, t, "REJECTED", actor, reason)
        return {"id": task_id, "state": "REJECTED"}

    @staticmethod
    def _check_live_lease(t: dict, agent: str) -> None:
        if t["lease_holder"] != agent:
            raise BoardError("not_lease_holder", f"only the lease holder ({t['lease_holder']}) can do this on {t['id']}")
        if t["lease_expires_at"] and t["lease_expires_at"] < _now():
            raise BoardError("lease_expired", f"your lease on {t['id']} expired at {t['lease_expires_at']:%H:%M} UTC; renew or re-claim")

    def _unmet_dependencies(self, cur, t: dict) -> list[str]:
        deps = t["contract"].get("depends_on") or []
        if not deps:
            return []
        cur.execute("SELECT id FROM mesh.tasks WHERE id = ANY(%s) AND state <> 'DONE'", (deps,))
        return [r[0] for r in cur.fetchall()]

    def claim(self, task_id: str, agent: str, ttl: timedelta | None = None) -> dict[str, Any]:
        """READY -> WORKING with a lease. Only the owner claims; dependencies must be DONE."""
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] != "READY":
                raise BoardError("bad_transition", f"{task_id} is {t['state']}, not READY")
            if agent != t["owner"] and not agent.startswith(f"{t['owner']}:"):  # e.g. duke:pairing (spec 10.4)
                raise BoardError("not_owner", f"{task_id} belongs to {t['owner']}")
            unmet = self._unmet_dependencies(cur, t)
            if unmet:
                raise BoardError("dependencies_open", f"{task_id} depends on unfinished tasks", unmet)
            if ttl is None:
                hours = (t["contract"].get("budget") or {}).get("wall_clock_h")
                ttl = timedelta(hours=float(hours)) if hours else DEFAULT_TOOL_LEASE
            exp = _now() + ttl
            cur.execute("UPDATE mesh.tasks SET lease_holder=%s, lease_expires_at=%s WHERE id=%s", (agent, exp, task_id))
            actor = base_principal(agent)
            self._set_state(cur, t, "WORKING", actor, "lease claimed", substate="pairing" if ":pairing" in agent else None)
            append_event(cur, actor, "task.claimed", {"task_id": task_id, "lease_holder": agent, "lease_expires_at": exp.isoformat()},
                         "task", task_id, correlation_id=task_id)
        return {"id": task_id, "state": "WORKING", "lease_expires_at": exp}

    def renew_lease(self, task_id: str, agent: str, ttl: timedelta = DEFAULT_LEASE) -> datetime:
        """Renewal is code, not LLM (spec 9.4): the host renews while a turn runs."""
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            if t["lease_holder"] != agent:
                raise BoardError("not_lease_holder", f"{agent} does not hold the lease on {task_id}")
            if t["lease_expires_at"] and t["lease_expires_at"] < _now():
                raise BoardError("lease_expired", f"lease on {task_id} already expired")
            exp = _now() + ttl
            cur.execute("UPDATE mesh.tasks SET lease_expires_at=%s WHERE id=%s", (exp, task_id))
            append_event(cur, base_principal(agent), "lease.renewed", {"task_id": task_id, "lease_expires_at": exp.isoformat()}, "task", task_id, correlation_id=task_id)
        return exp

    def expire_leases(self, now: datetime | None = None) -> list[str]:
        """WORKING tasks whose lease lapsed go back to READY; Frank is alerted (spec 9.4). Deterministic sweep."""
        now = now or _now()
        expired = []
        with self._tx() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM mesh.tasks WHERE state='WORKING' AND lease_expires_at < %s FOR UPDATE SKIP LOCKED", (now,))
            for (tid,) in cur.fetchall():
                t = self._task_row(cur, tid)
                holder = t["lease_holder"]
                cur.execute("UPDATE mesh.tasks SET lease_holder=NULL, lease_expires_at=NULL WHERE id=%s", (tid,))
                # WORKING -> READY is not an agent transition; it is the kernel reclaiming the lease.
                cur.execute("UPDATE mesh.tasks SET state='READY', updated_at=now(), version=version+1 WHERE id=%s", (tid,))
                append_event(cur, "frank", "lease.expired", {"task_id": tid, "holder": holder, "alert": "frank"}, "task", tid, correlation_id=tid)
                expired.append(tid)
        return expired

    def transition(self, task_id: str, to: str, actor: str, reason: str, substate: str | None = None) -> dict[str, Any]:
        """Ungated lifecycle moves (e.g. WORKING -> INPUT_REQUIRED). Gated moves use their own methods."""
        if to not in STATES:
            raise BoardError("bad_state", f"unknown state {to!r}")
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            frm = t["state"]
            if to not in TRANSITIONS.get(frm, set()):
                raise BoardError("bad_transition", f"{frm} -> {to} is not in the task lifecycle (spec 9.4)")
            if (frm, to) in GATED:
                raise BoardError("gated_transition", f"{frm} -> {to} goes through its gate (make_ready / claim / submit_completion / verifier)")
            if frm in ("WORKING", "INPUT_REQUIRED", "AUTH_REQUIRED") and actor != "logan":
                # (the lease holder may be an owner's pairing session, e.g. duke:pairing)
                self._check_live_lease(t, actor)
            if frm in ("BLOCKED", "PARKED") and actor not in (t["owner"], "logan"):
                raise BoardError("not_owner", f"only the owner or the Boss can return {task_id} to READY")
            if to in ("BLOCKED", "PARKED", "FAILED", "CANCELED"):
                cur.execute("UPDATE mesh.tasks SET lease_holder=NULL, lease_expires_at=NULL WHERE id=%s", (task_id,))
            actor = base_principal(actor)
            self._set_state(cur, t, to, actor, reason, substate=substate)
            if to == "BLOCKED":
                append_event(cur, actor, "task.blocked", {"task_id": task_id, "reason": reason}, "task", task_id, correlation_id=task_id)
            if to in ("FAILED", "CANCELED"):
                append_event(cur, actor, f"task.{to.lower()}", {"task_id": task_id, "reason": reason}, "task", task_id, correlation_id=task_id)
        return {"id": task_id, "state": to}

    def submit_completion(self, task_id: str, agent: str, report: dict[str, Any]) -> dict[str, Any]:
        """DONE report (spec 9.11): WORKING -> IN_REVIEW only through the receipts gate. On failure
        the DONE is rejected with the exact missing/invalid receipts; the task stays WORKING."""
        from .receipts_gate import ReceiptsGate

        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] != "WORKING":
                raise BoardError("bad_transition", f"{task_id} is {t['state']}; only WORKING tasks submit completion")
            self._check_live_lease(t, agent)

            def fetch(rid: str):
                cur.execute("SELECT task_id, tool, status, check_id, ended_at FROM mesh.receipts WHERE id=%s", (rid,))
                r = cur.fetchone()
                return dict(zip(["task_id", "tool", "status", "check_id", "ended_at"], r)) if r else None

            ok, problems = ReceiptsGate(fetch).check(task_id, report, t["contract"].get("acceptance_checks") or [],
                                                     t["contract_frozen_at"])
            actor = base_principal(agent)
            if not ok:
                append_event(cur, actor, "task.completion_rejected", {"task_id": task_id, "problems": problems},
                             "task", task_id, correlation_id=task_id)
                conn.commit()
                raise BoardError("receipts_gate", f"DONE rejected: {len(problems)} problem(s) with the evidence", problems)
            self._set_state(cur, t, "IN_REVIEW", actor, "completion report passed the receipts gate")
            append_event(cur, actor, "task.completion_submitted", {"task_id": task_id, "report": report}, "task", task_id, correlation_id=task_id)
            append_event(cur, actor, "verification.started", {"task_id": task_id, "owner": t["owner"]}, "task", task_id, correlation_id=task_id)
        return {"id": task_id, "state": "IN_REVIEW"}

    def verification_failed(self, task_id: str, verifier: str, report: dict[str, Any]) -> dict[str, Any]:
        """IN_REVIEW -> WORKING with the findings; the owner is woken P1 (spec 9.11 step 4).
        A second FAIL on the same task triggers a replan (spec 9.9)."""
        with self._tx() as conn, conn.cursor() as cur:
            if self._principal(cur, verifier).kind != "service":
                raise BoardError("not_verifier", "only the verifier service fails a review")
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] != "IN_REVIEW":
                raise BoardError("bad_transition", f"{task_id} is {t['state']}, not IN_REVIEW")
            cur.execute("UPDATE mesh.tasks SET lease_expires_at=%s WHERE id=%s", (_now() + DEFAULT_TOOL_LEASE, task_id))
            self._set_state(cur, t, "WORKING", verifier, "verifier FAIL")
            append_event(cur, verifier, "verification.failed", {"task_id": task_id, "owner": t["owner"], "report": report},
                         "task", task_id, correlation_id=task_id)
            cur.execute("SELECT count(*) FROM mesh.events WHERE subject_type='task' AND subject_id=%s AND type='verification.failed'", (task_id,))
            fails = cur.fetchone()[0]
            if fails >= 2:
                append_event(cur, verifier, "task.replan_needed", {"task_id": task_id, "owner": t["owner"], "reason": f"verifier failed {fails} times"},
                             "task", task_id, correlation_id=task_id)
        return {"id": task_id, "state": "WORKING", "verifier_fails": fails}

    def mark_done(self, task_id: str, verifier: str, verification: dict) -> dict[str, Any]:
        """IN_REVIEW -> DONE only on a verifier PASS (spec 9.11). Emits task.unblocked for dependents."""
        if verification.get("verdict") != "PASS":
            raise BoardError("not_passed", "DONE requires a verifier PASS")
        with self._tx() as conn, conn.cursor() as cur:
            p = self._principal(cur, verifier)
            if p.kind != "service" or not verifier.startswith("svc-verifier"):
                raise BoardError("not_verifier", "only the verifier service marks a task DONE (separation of duties, 14.2)")
            t = self._task_row(cur, task_id, lock=True)
            if t["state"] != "IN_REVIEW":
                raise BoardError("bad_transition", f"{task_id} is {t['state']}, not IN_REVIEW")
            cur.execute("UPDATE mesh.tasks SET lease_holder=NULL, lease_expires_at=NULL WHERE id=%s", (task_id,))
            self._set_state(cur, t, "DONE", verifier, "verifier PASS")
            append_event(cur, verifier, "task.done", {"task_id": task_id}, "task", task_id, correlation_id=task_id)
            cur.execute("SELECT e.to_id, t.owner FROM mesh.task_edges e JOIN mesh.tasks t ON t.id=e.to_id "
                        "WHERE e.from_id=%s AND e.kind='blocks'", (task_id,))
            for dep_id, owner in cur.fetchall():
                dep = self._task_row(cur, dep_id)
                if not self._unmet_dependencies(cur, dep):
                    append_event(cur, "frank", "task.unblocked", {"task_id": dep_id, "owner": owner, "by": task_id}, "task", dep_id, correlation_id=dep_id)
        return {"id": task_id, "state": "DONE"}

    # ── ledgers (spec 9.8, 16.3) ─────────────────────────────────────────────
    LEDGER_REQUIRED = ("objective_restated", "plan", "progress", "next_step")

    def ledger_update(self, task_id: str, agent: str, body: dict[str, Any]) -> int:
        """Replace the ledger with a new version; the old one goes to history. Lease holder only."""
        missing = [k for k in self.LEDGER_REQUIRED if k not in body]
        if missing:
            raise BoardError("ledger_invalid", "ledger missing required fields", missing)
        if not isinstance(body.get("plan"), list):
            raise BoardError("ledger_invalid", "plan must be a list of steps")
        bad = [i for i, st in enumerate(body["plan"])
               if not isinstance(st, dict) or not isinstance(st.get("what", ""), str)
               or st.get("status") not in PLAN_STATUSES or not isinstance(st.get("receipts", []), list)]
        if bad:
            raise BoardError("ledger_invalid",
                             'each plan step is an object {"step": n, "what": "...", "status": "todo|doing|done|blocked", "receipts": []}',
                             [f"plan[{i}]" for i in bad])
        if not isinstance(body.get("progress"), dict):
            raise BoardError("ledger_invalid", "progress must be an object")
        body = json.loads(json.dumps(body))  # detach
        progress = dict(body.get("progress") or {})
        progress.pop("is_looping", None)  # Ballast's field, never the agent's
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id, lock=True)
            self._check_live_lease(t, agent)
            cur.execute("SELECT version, body, updated_by, updated_at FROM mesh.ledgers WHERE task_id=%s FOR UPDATE", (task_id,))
            row = cur.fetchone()
            version = (row[0] + 1) if row else 1
            prev_looping = ((row[1] or {}).get("progress") or {}).get("is_looping") if row else None
            if prev_looping is not None:
                progress["is_looping"] = prev_looping
            body.update({"task_id": task_id, "version": version, "progress": progress, "updated_at": _now().isoformat()})
            if row:
                cur.execute("INSERT INTO mesh.ledger_history (task_id, version, body, updated_by, updated_at) VALUES (%s,%s,%s,%s,%s)",
                            (task_id, row[0], Jsonb(row[1]), row[2], row[3]))
                cur.execute("UPDATE mesh.ledgers SET version=%s, body=%s, updated_by=%s, updated_at=now() WHERE task_id=%s",
                            (version, Jsonb(body), agent, task_id))
            else:
                cur.execute("INSERT INTO mesh.ledgers (task_id, version, body, updated_by) VALUES (%s,%s,%s,%s)",
                            (task_id, version, Jsonb(body), agent))
            append_event(cur, base_principal(agent), "task.ledger_updated", {"task_id": task_id, "version": version, "next_step": body.get("next_step")},
                         "task", task_id, correlation_id=task_id)
        return version

    def set_looping(self, task_id: str, is_looping: bool) -> None:
        """Ballast only (spec 9.8): the loop flag is computed from turns, not claimed by agents."""
        with self._tx() as conn, conn.cursor() as cur:
            cur.execute("UPDATE mesh.ledgers SET body = jsonb_set(body, '{progress,is_looping}', %s::jsonb) WHERE task_id=%s",
                        (json.dumps(bool(is_looping)), task_id))
            if is_looping:
                append_event(cur, "frank", "loop.detected", {"task_id": task_id}, "task", task_id, correlation_id=task_id)

    def ledger(self, task_id: str) -> dict[str, Any] | None:
        with self._tx() as conn, conn.cursor() as cur:
            cur.execute("SELECT body FROM mesh.ledgers WHERE task_id=%s", (task_id,))
            row = cur.fetchone()
        return row[0] if row else None

    # ── messages (spec 8.3-8.5, 8.11) ────────────────────────────────────────
    def send(self, message: dict[str, Any]) -> dict[str, Any]:
        """Validate and store an A2A message; the kernel records delivery, never the agent.

        A REQUEST with taskId "new" creates a PROPOSED task for the recipient (spec 8.4),
        traced to the mandate/objective the sender supplies in metadata.
        """
        sa = (message.get("metadata") or {}).get(SPEECH_ACT_EXT) or {}
        sender = sa.get("from")
        with self._tx() as conn, conn.cursor() as cur:
            self._principal(cur, sender or "")
            cur.execute("SELECT id FROM mesh.principals WHERE active")
            known = {r[0] for r in cur.fetchall()}

            # Idempotent by messageId (spec 8.5), checked FIRST so a retried REQUEST-for-new-work
            # never creates a second task. The advisory lock serializes concurrent retries.
            mid = message.get("messageId") or ""
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("msg:" + mid,))
            cur.execute("SELECT task_id FROM mesh.messages WHERE id=%s", (mid,))
            dup = cur.fetchone()
            if dup:
                return {"id": mid, "task_id": dup[0], "duplicate": True}

            if sa.get("act") == "REQUEST" and message.get("taskId") == "new":
                new = sa.get("new_task") or {}
                # Validate everything else first, so a bad envelope never leaves an orphan task.
                probe = {**message, "taskId": "T-0000", "contextId": "ctx-T-0000"}
                pre_ok, pre_errors = validate_envelope(probe, known_agents=known)
                if not pre_ok:
                    raise BoardError("invalid_message", "message rejected", sorted(set(pre_errors)))
                if not (new.get("mandate_id") or new.get("objective_id")) and new.get("type") not in UNTRACED_OK:
                    raise BoardError("untraceable", "a REQUEST for new work names the mandate or objective it serves (spec 9.1)")
                # task + contract + message commit together or not at all
                created = self.create_task(title=new.get("title") or sa.get("purpose", "")[:90], type=new.get("type", "build"),
                                           created_by=sender, owner=(sa.get("to") or [None])[0],
                                           mandate_id=new.get("mandate_id"), objective_id=new.get("objective_id"), cur=cur)
                self.propose(created["id"], new.get("contract") or {}, sender, cur=cur)
                message = {**message, "taskId": created["id"], "contextId": created["context_id"]}

            ok, errors = validate_envelope(message, known_agents=known)
            if message.get("taskId"):
                cur.execute("SELECT context_id FROM mesh.tasks WHERE id=%s", (message["taskId"],))
                row = cur.fetchone()
                if not row:
                    errors.append(f"taskId {message['taskId']} does not exist")
                elif row[0] != message.get("contextId"):
                    errors.append(f"contextId {message.get('contextId')} does not match task {message['taskId']} ({row[0]})")
            text = " ".join(p.get("text", "") for p in message.get("parts") or [] if isinstance(p, dict))
            warning = None
            acts_in_body = _ACT_LINE.findall(text)
            if len(acts_in_body) > 1:
                cur.execute("SELECT 1 FROM mesh.lint_warnings WHERE sender=%s AND rule='one_intent' AND context_id=%s",
                            (sender, message.get("contextId") or ""))
                if cur.fetchone():
                    errors.append(f"one intent per message: found {len(acts_in_body)} acts ({', '.join(acts_in_body)}); send them separately")
                else:
                    warning = f"one intent per message: found {len(acts_in_body)} acts; next time this is rejected"
                    cur.execute("INSERT INTO mesh.lint_warnings (sender, rule, context_id) VALUES (%s,'one_intent',%s) ON CONFLICT DO NOTHING",
                                (sender, message.get("contextId") or ""))
            if errors or not ok:
                raise BoardError("invalid_message", "message rejected", sorted(set(errors)))
            taint = ((message.get("metadata") or {}).get("https://blimp.my/a2a/ext/taint/v1") or {}).get("tainted", False)
            refs = {k: sa.get(k) for k in ("decision_refs", "artifact_refs", "receipt_refs") if sa.get(k)}
            if message.get("referenceTaskIds"):
                refs["task_refs"] = message["referenceTaskIds"]
            cur.execute(
                """INSERT INTO mesh.messages (id, context_id, task_id, sender, recipients, cc, act, expects, priority, reply_by,
                   purpose, parts, refs, tainted, visibility, delivered_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())""",
                (message["messageId"], message["contextId"], message["taskId"], sender, sa.get("to"), sa.get("cc") or [],
                 sa["act"], sa.get("expects", "none"), sa.get("priority", "P2"), sa.get("reply_by"), sa.get("purpose", ""),
                 Jsonb(message.get("parts") or []), Jsonb(refs), bool(taint), sa.get("visibility", "thread")),
            )
            if sa.get("in_reply_to"):
                cur.execute("UPDATE mesh.messages SET answered_by=%s WHERE id=%s AND answered_by IS NULL", (message["messageId"], sa["in_reply_to"]))
            ev = append_event(cur, sender, "message.sent", {"message": message}, "message", message["messageId"], correlation_id=message["taskId"])
        return {"id": message["messageId"], "task_id": message["taskId"], "event_id": ev["id"], "warning": warning}

    def mark_read(self, message_ids: list[str], agent: str) -> int:
        """Read receipts come from code (spec 8.5): set when a host includes the message in a context pack."""
        with self._tx() as conn, conn.cursor() as cur:
            cur.execute("UPDATE mesh.messages SET read_at=now() WHERE id = ANY(%s) AND %s = ANY(recipients) AND read_at IS NULL RETURNING id",
                        (message_ids, agent))
            ids = [r[0] for r in cur.fetchall()]
            for mid in ids:
                append_event(cur, agent, "message.read", {"message_id": mid}, "message", mid)
        return len(ids)

    def inbox(self, agent: str, unread_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
        with self._tx() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, task_id, sender, act, priority, purpose, parts, reply_by, created_at, read_at FROM mesh.messages "
                "WHERE %s = ANY(recipients) " + ("AND read_at IS NULL " if unread_only else "") +
                "ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END, created_at LIMIT %s",
                (agent, limit),
            )
            keys = ["id", "task_id", "from", "act", "priority", "purpose", "parts", "reply_by", "created_at", "read_at"]
            return [dict(zip(keys, r)) for r in cur.fetchall()]

    # ── reads ────────────────────────────────────────────────────────────────
    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._tx() as conn, conn.cursor() as cur:
            t = self._task_row(cur, task_id)
        t["a2a_state"] = A2A_STATE[t["state"]]
        return t

    def list_tasks(self, owner: str | None = None, states: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        q = "SELECT id, title, type, state, owner, priority, pinned, updated_at FROM mesh.tasks WHERE true"
        args: list[Any] = []
        if owner:
            q += " AND owner=%s"
            args.append(owner)
        if states:
            q += " AND state = ANY(%s)"
            args.append(states)
        q += " ORDER BY pinned DESC, priority DESC, updated_at DESC LIMIT %s"
        args.append(limit)
        with self._tx() as conn, conn.cursor() as cur:
            cur.execute(q, args)
            keys = ["id", "title", "type", "state", "owner", "priority", "pinned", "updated_at"]
            return [dict(zip(keys, r)) for r in cur.fetchall()]
