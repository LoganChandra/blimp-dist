"""
broker.py — Pure-FastAPI A2A broker. Implements A2A wire format directly.

Why not the a2a-sdk: API churn between v0.3 and v1.0, Python 3.14 compat issues.
The A2A spec is just JSON-RPC 2.0 over HTTP with a known Agent Card schema —
we implement that directly so we don't fight the SDK.

Endpoints (per A2A spec v1.0):
  GET  /{agent}/.well-known/agent-card.json   → AgentCard
  POST /{agent}/                              → JSON-RPC message/send

Lifecycle enforcement (P0.6 + P1.5):
  POST /tasks/{task_id}/transition  → enforce ALLOWED_TRANSITIONS, bounce budget,
                                      and completion-payload schema. Illegal jumps
                                      get HTTP 409; non-conforming completions are
                                      rejected and count as a bounce; the 2nd reject
                                      auto-transitions the task to `failed`.
  GET  /tasks/stale                 → submitted >10min without working, and
                                      input-required >2h. Designed for a
                                      curl+jq one-liner (the watchdog polls it).

NOTIFICATION (zero-idle-token design):
  When a task arrives, the broker immediately pushes to the target agent
  so agents never need to poll (which would burn tokens when idle).
  - Duke:  tmux send-keys into Duke's Claude Code session
  - Hermes: POST to Hermes webhook (gateway session triggered only when notified)
"""

from __future__ import annotations
import json
import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [broker] %(message)s")
log = logging.getLogger("broker")

DB_URL = os.environ["DUKE_DB_URL"]
# Loopback by default (P0.2 security triage). Override with BIND_HOST (or the
# legacy A2A_BROKER_HOST) only for a Tailscale-interface bind — never 0.0.0.0.
HOST = os.environ.get("BIND_HOST") or os.environ.get("A2A_BROKER_HOST", "127.0.0.1")
PORT = int(os.environ.get("A2A_BROKER_PORT", "9091"))
PUBLIC_URL_BASE = os.environ.get("A2A_BROKER_PUBLIC_URL", f"http://localhost:{PORT}")

# Agent notification config
# Duke: tmux session name
DUKE_TMUX_SESSION = os.environ.get("DUKE_TMUX_SESSION", "duke")
# Hermes: webhook URL (set up via `hermes webhook subscribe a2a-task`)
HERMES_WEBHOOK_URL = os.environ.get("HERMES_WEBHOOK_URL", "http://localhost:8644/webhooks/a2a-task")
# Shared webhook secret — injected via EnvironmentFile= from gitignored
# blimp-infra/secrets/a2a-secret.env. No hardcoded fallback: a known default
# in a committed file is a secret in git (P0.2 security triage).
WEBHOOK_SECRET = os.environ.get("BROKER_HERMES_SECRET", "")
if not WEBHOOK_SECRET:
    log.warning("BROKER_HERMES_SECRET not set — Hermes webhook notifications "
                "will fail signature verification (see blimp-infra/secrets/)")

# ─── Lifecycle (P0.6 + P1.5) ────────────────────────────────────────────
# A2A task states. The DB already declares these (postgres/schema.sql); this is
# the broker-side ENFORCEMENT of legal moves between them.
#   submitted → working → input-required ⇄ working → completed/failed
# Terminal states (completed/failed/canceled) have no outgoing transitions.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "submitted":      {"working", "input-required", "completed", "failed", "canceled"},
    "working":        {"working", "input-required", "completed", "failed", "canceled"},
    "input-required": {"working", "input-required", "completed", "failed", "canceled"},
    "completed":      set(),
    "failed":         set(),
    "canceled":       set(),
}
ALL_STATES = set(ALLOWED_TRANSITIONS.keys())
TERMINAL_STATES = {s for s, nexts in ALLOWED_TRANSITIONS.items() if not nexts}

# Bounce budget: a completion rejected this many times auto-fails the task.
BOUNCE_BUDGET = int(os.environ.get("A2A_BOUNCE_BUDGET", "2"))

# Watchdog thresholds (seconds). The /tasks/stale endpoint uses these.
STALE_SUBMITTED_SECS = int(os.environ.get("A2A_STALE_SUBMITTED_SECS", str(10 * 60)))   # 10 min
STALE_INPUT_REQ_SECS = int(os.environ.get("A2A_STALE_INPUT_REQ_SECS", str(2 * 60 * 60)))  # 2 h


def db_conn():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def run_migration():
    """Idempotent startup migration. Adds lifecycle bookkeeping columns the
    P0.6/P1.5 enforcement needs. Safe to run on every boot — ADD COLUMN IF NOT
    EXISTS is a no-op once applied. The state column + its allowed values are
    already declared in postgres/schema.sql; this only adds the bookkeeping."""
    with db_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE a2a_messages
                ADD COLUMN IF NOT EXISTS bounce_count      INTEGER     NOT NULL DEFAULT 0,
                ADD COLUMN IF NOT EXISTS fail_reason       TEXT,
                ADD COLUMN IF NOT EXISTS working_at        TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS input_required_at TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS completed_at      TIMESTAMPTZ,
                ADD COLUMN IF NOT EXISTS failed_at         TIMESTAMPTZ
            """
        )
        c.commit()
    log.info("migration ok (lifecycle columns present)")


# Maps a state to the per-transition timestamp column stamped on entry.
_TS_COLUMN = {
    "working":        "working_at",
    "input-required": "input_required_at",
    "completed":      "completed_at",
    "failed":         "failed_at",
}


def insert_message(*, task_id, parent_task_id, from_agent, to_agent,
                   state, body, skill=None):
    with db_conn() as c, c.cursor() as cur:
        cur.execute(
            """INSERT INTO a2a_messages
               (task_id, parent_task_id, from_agent, to_agent, state, skill, body)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            [task_id, parent_task_id, from_agent, to_agent, state, skill,
             json.dumps(body)],
        )
        row = cur.fetchone()
        c.commit()
        return row["id"]


def _fetch_root(cur, task_id):
    """The canonical task row (the original submission, parent_task_id IS NULL)."""
    cur.execute(
        "SELECT * FROM a2a_messages "
        "WHERE task_id = %s AND parent_task_id IS NULL LIMIT 1",
        [task_id],
    )
    return cur.fetchone()


# ─── Completion-payload schema (P1.5) ───────────────────────────────────
def validate_completion(payload) -> list[str]:
    """Validate a completion payload against {status, artifacts[], verification:{cmd, output}}.
    Returns a list of error strings — empty list means valid. Hermes runs on a
    cheap model that fabricates tool calls; this gate makes "done" carry evidence."""
    errs: list[str] = []
    if not isinstance(payload, dict):
        return ["completion payload must be a JSON object"]
    if "status" not in payload:
        errs.append("missing 'status'")
    elif not isinstance(payload["status"], str) or not payload["status"].strip():
        errs.append("'status' must be a non-empty string")

    if "artifacts" not in payload:
        errs.append("missing 'artifacts'")
    elif not isinstance(payload["artifacts"], list):
        errs.append("'artifacts' must be a list")

    if "verification" not in payload:
        errs.append("missing 'verification'")
    else:
        v = payload["verification"]
        if not isinstance(v, dict):
            errs.append("'verification' must be an object with {cmd, output}")
        else:
            if not isinstance(v.get("cmd"), str) or not v.get("cmd", "").strip():
                errs.append("'verification.cmd' must be a non-empty string")
            if not isinstance(v.get("output"), str) or not v.get("output", "").strip():
                errs.append("'verification.output' must be a non-empty string")
    return errs


# ─── Zero-idle-token notifications ──────────────────────────────────────

def _tmux_submit(session: str, prompt: str) -> None:
    """Type a prompt into a Claude Code tmux session AND actually submit it.

    The text and the Enter MUST be two separate send-keys calls with a beat between:
    a combined `send-keys "text" Enter` races Claude Code's TUI paste-ingest, so the
    Enter registers as a NEWLINE in the composer instead of a submit. That was the bug
    (2026-07-07) — A2A prompts sat in Duke's box until a human hit Enter, and because
    the task was never read/acked the relay re-poked it every 6s forever (wasted usage
    the moment it ever submitted). `-l --` sends the text literally.
    """
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "-l", "--", prompt],
        timeout=5, capture_output=True,
    )
    time.sleep(0.4)
    subprocess.run(
        ["tmux", "send-keys", "-t", session, "Enter"],
        timeout=5, capture_output=True,
    )


def _notify_duke(text: str, task_id: str):
    """Inject a prompt into Duke's tmux session AND submit it. Fire-and-forget."""
    prompt = (
        f"You have a new A2A task (task_id={task_id}). "
        f"Check your inbox with mcp__blimp-brain__a2a_inbox (state='any', limit=5), "
        f"then respond with mcp__blimp-brain__a2a_reply. "
        f"Task summary: {text[:200]}"
    )
    try:
        _tmux_submit(DUKE_TMUX_SESSION, prompt)
        log.info("notified duke via tmux (task_id=%s)", task_id)
    except Exception as e:
        log.warning("duke tmux notify failed: %s", e)


def _notify_hermes(text: str, task_id: str):
    """POST to Hermes webhook with GitHub-compatible HMAC signature."""
    import hashlib, hmac
    payload = json.dumps({
        "task_id": task_id,
        "instruction": (
            f"You have a new A2A task (task_id={task_id}). "
            f"Check your inbox with mcp__blimp-brain__a2a_inbox (state='any', limit=5), "
            f"then respond with mcp__blimp-brain__a2a_reply. "
            f"Task summary: {text[:200]}"
        ),
    }).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    try:
        r = httpx.post(
            HERMES_WEBHOOK_URL,
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": f"sha256={signature}",
            },
            timeout=5,
        )
        log.info("notified hermes via webhook (task_id=%s, status=%s)", task_id, r.status_code)
    except Exception as e:
        log.warning("hermes webhook notify failed: %s", e)


def _notify_tmux(session: str, text: str, task_id: str):
    """Inject a terse nudge (NOT the task body) into a tmux agent's session + submit."""
    prompt = (
        f"You have a new A2A task (task_id={task_id}). "
        f"Check your inbox with mcp__blimp-brain__a2a_inbox (state='any', limit=5), "
        f"then respond with mcp__blimp-brain__a2a_reply. Summary: {text[:160]}"
    )
    _tmux_submit(session, prompt)


# ─── Mailroom: presence-gated delivery + relay (busy-gate is the Boss's #1) ──
TMUX_SESSIONS = {"duke": DUKE_TMUX_SESSION, "nanook": os.environ.get("NANOOK_TMUX_SESSION", "nanook")}
RELAY_INTERVAL = int(os.environ.get("A2A_RELAY_INTERVAL", "6"))
_BUSY_SIGNS = ("esc to interrupt", "esc to cancel", "tokens · esc")


def _pane_busy(session: str) -> bool:
    """Coarse tmux pane veto. True if the pane clearly shows an active turn.
    Fail-closed (busy) on any error — never risk injecting into a working session."""
    try:
        r = subprocess.run(["tmux", "capture-pane", "-p", "-t", session],
                           capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            return True
        low = r.stdout.lower()
        return any(s in low for s in _BUSY_SIGNS)
    except Exception:
        return True


def _deliverable(agent: str) -> bool:
    """May we ring `agent`'s doorbell right now? Respect explicit busy/offline
    presence; for tmux agents also apply the live pane veto. Does NOT block on a
    stale heartbeat (presence hooks may not be wired yet — pane veto is the live gate)."""
    try:
        with db_conn() as c, c.cursor() as cur:
            cur.execute("SELECT status FROM agent_presence WHERE agent=%s", [agent])
            row = cur.fetchone()
        if row and row["status"] in ("busy", "offline"):
            return False
        sess = TMUX_SESSIONS.get(agent)
        if sess and _pane_busy(sess):
            return False
        return True
    except Exception:
        return True


def _dispatch(agent: str, text: str, task_id: str) -> bool:
    """Actually ring the doorbell. Returns True if dispatched."""
    try:
        if agent in ("duke", "nanook"):
            _notify_tmux(TMUX_SESSIONS[agent], text, task_id)
        elif agent == "hermes":
            _notify_hermes(text, task_id)
        else:
            return False
        return True
    except Exception as e:
        log.warning("dispatch %s failed: %s", agent, e)
        return False


def notify_agent(agent: str, text: str, task_id: str) -> bool:
    """Route a notification, GATED on recipient presence — never inject into a busy
    session. Returns True only if a notification was actually dispatched."""
    if agent not in ("duke", "hermes", "nanook"):
        log.warning("no notification channel for agent: %s", agent)
        return False
    if not _deliverable(agent):
        log.info("notify deferred: %s busy/offline (task=%s)", agent, task_id)
        return False
    return _dispatch(agent, text, task_id)


def _msg_text(body) -> str:
    try:
        return "\n".join(p.get("text", "") for p in body["params"]["message"]["parts"]
                         if p.get("kind") == "text").strip()
    except Exception:
        return ""


def relay_once():
    """Re-ring the doorbell for queued+undelivered tasks whose recipient is now
    deliverable, with exponential backoff. Never re-notifies a task the recipient
    already read (acked_at set). FOR UPDATE SKIP LOCKED = safe under concurrency."""
    with db_conn() as c, c.cursor() as cur:
        cur.execute(
            """SELECT id, task_id, to_agent, body, attempt_count
               FROM a2a_messages
               WHERE parent_task_id IS NULL AND state='submitted' AND acked_at IS NULL
                 AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
               ORDER BY created_at ASC
               FOR UPDATE SKIP LOCKED LIMIT 20""")
        for r in cur.fetchall():
            agent, n = r["to_agent"], r["attempt_count"] + 1
            if not _deliverable(agent):
                cur.execute("UPDATE a2a_messages SET next_attempt_at=NOW()+INTERVAL '15 seconds', attempt_count=%s WHERE id=%s", [n, r["id"]])
                continue
            if _dispatch(agent, _msg_text(r["body"]), r["task_id"]):
                cur.execute("UPDATE a2a_messages SET notified_at=COALESCE(notified_at,NOW()), attempt_count=%s, next_attempt_at=NOW()+INTERVAL '45 seconds', last_error=NULL WHERE id=%s", [n, r["id"]])
            else:
                back = min(300, 5 * (2 ** min(n, 6)))
                cur.execute("UPDATE a2a_messages SET attempt_count=%s, next_attempt_at=NOW()+make_interval(secs=>%s), last_error='notify failed' WHERE id=%s", [n, back, r["id"]])
        c.commit()


def _drive_mirror():
    """Drive the Discord receipt mirror (dry-run unless MIRROR_LIVE=1)."""
    try:
        import discord_mirror
        with discord_mirror._conn() as mc:
            discord_mirror.drain_once(mc)
    except Exception as e:
        log.warning("mirror drive failed: %s", e)


async def relay_loop():
    import asyncio
    log.info("Mailroom relay started (interval=%ss)", RELAY_INTERVAL)
    while True:
        try:
            relay_once()
        except Exception as e:
            log.warning("relay_once error: %s", e)
        _drive_mirror()
        await asyncio.sleep(RELAY_INTERVAL)


# ─── Agent Card content (per LF A2A v1.0 spec) ───────────────────────
AGENT_CARDS = {
    "duke": {
        "name": "Blimp Duke",
        "description": "Implementation Lead — coordination, shipping, verification of designs.",
        "url": f"{PUBLIC_URL_BASE}/duke/",
        "version": "0.1.0",
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "ship-implementation", "name": "Ship implementation",
             "description": "Translate brief into a delivered build.",
             "tags": ["shipping", "delivery"]},
            {"id": "verify-design", "name": "Verify design",
             "description": "Review proposed design for shippability. Returns APPROVED/NEEDS_REVISION/BLOCKED_NEEDS_LOGAN.",
             "tags": ["review", "verify"]},
        ],
    },
    "hermes": {
        "name": "Blimp Hermes",
        "description": "Technical Lead — architecture, tech stack, technical direction.",
        "url": f"{PUBLIC_URL_BASE}/hermes/",
        "version": "0.1.0",
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "capabilities": {"streaming": False, "pushNotifications": False},
        "skills": [
            {"id": "design-architecture", "name": "Design architecture",
             "description": "Propose technical design for a build.",
             "tags": ["design", "architecture"]},
            {"id": "review-design", "name": "Review design",
             "description": "Audit a design for technical soundness.",
             "tags": ["review"]},
        ],
    },
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run the idempotent lifecycle migration on boot + start the Mailroom relay."""
    import asyncio
    try:
        run_migration()
    except Exception as e:
        # Don't crash the broker if the DB is momentarily unavailable; the
        # message-send path will surface the error on first use.
        log.warning("startup migration deferred: %s", e)
    relay_task = asyncio.create_task(relay_loop())
    try:
        yield
    finally:
        relay_task.cancel()


app = FastAPI(title="Blimp A2A broker", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"ok": True, "agents": list(AGENT_CARDS.keys())}


@app.get("/{agent}/.well-known/agent-card.json")
def agent_card(agent: str):
    if agent not in AGENT_CARDS:
        raise HTTPException(404, f"unknown agent: {agent}")
    return AGENT_CARDS[agent]


# ─── Lifecycle transition endpoint (P0.6 + P1.5) ────────────────────────
@app.post("/tasks/{task_id}/transition")
async def transition(task_id: str, request: Request):
    """Enforce a state transition on a task.

    Body: {"to": "<state>", "result": <obj?>, "reason": <str?>}

    Rules:
      - Unknown target state            → 400
      - Illegal transition from current → 409 (Conflict)
      - to == "completed": `result` must validate against the P1.5 schema
        {status, artifacts[], verification:{cmd, output}}. A non-conforming
        completion is REJECTED and counts as a bounce. The Nth bounce
        (N == BOUNCE_BUDGET) auto-transitions the task to `failed`.
    Backward-compatible: the legacy message/send flow and the MCP direct-DB
    writes are untouched; this is an additional, opt-in enforcement surface.
    """
    body = await request.json()
    to_state = body.get("to")
    if to_state not in ALL_STATES:
        raise HTTPException(400, f"unknown state: {to_state!r} (valid: {sorted(ALL_STATES)})")

    with db_conn() as c, c.cursor() as cur:
        root = _fetch_root(cur, task_id)
        if not root:
            raise HTTPException(404, f"task {task_id} not found")
        cur_state = root["state"]

        # Already-terminal tasks accept no further transitions.
        if cur_state in TERMINAL_STATES:
            raise HTTPException(
                409,
                f"task {task_id} is terminal ({cur_state}); no transition to {to_state} allowed",
            )
        if to_state not in ALLOWED_TRANSITIONS[cur_state]:
            raise HTTPException(
                409,
                f"illegal transition {cur_state} → {to_state} "
                f"(allowed from {cur_state}: {sorted(ALLOWED_TRANSITIONS[cur_state])})",
            )

        # ── Completion schema gate (P1.5) ──
        if to_state == "completed":
            errs = validate_completion(body.get("result"))
            if errs:
                new_bounce = root["bounce_count"] + 1
                if new_bounce >= BOUNCE_BUDGET:
                    # Budget exhausted → auto-fail (never livelock a fabricator).
                    reason = (
                        f"completion rejected {new_bounce}x (budget {BOUNCE_BUDGET}); "
                        f"last errors: {'; '.join(errs)}"
                    )
                    cur.execute(
                        "UPDATE a2a_messages SET state='failed', bounce_count=%s, "
                        "fail_reason=%s, failed_at=NOW(), updated_at=NOW() "
                        "WHERE task_id=%s AND parent_task_id IS NULL",
                        [new_bounce, reason, task_id],
                    )
                    c.commit()
                    log.warning("task %s auto-failed after %s bounces", task_id, new_bounce)
                    return JSONResponse(
                        status_code=409,
                        content={
                            "ok": False, "task_id": task_id, "state": "failed",
                            "bounce_count": new_bounce, "auto_failed": True,
                            "reason": reason, "errors": errs,
                        },
                    )
                # Bounce: record it, keep the task open for a retry.
                cur.execute(
                    "UPDATE a2a_messages SET bounce_count=%s, updated_at=NOW() "
                    "WHERE task_id=%s AND parent_task_id IS NULL",
                    [new_bounce, task_id],
                )
                c.commit()
                log.info("task %s completion bounced (%s/%s)", task_id, new_bounce, BOUNCE_BUDGET)
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False, "task_id": task_id, "state": cur_state,
                        "bounce_count": new_bounce, "auto_failed": False,
                        "errors": errs,
                        "schema": {"status": "str", "artifacts": "list",
                                   "verification": {"cmd": "str", "output": "str"}},
                    },
                )

        # ── Legal transition (and, for completed, a conforming payload) ──
        sets = ["state=%s", "updated_at=NOW()"]
        vals: list = [to_state]
        ts_col = _TS_COLUMN.get(to_state)
        if ts_col:
            sets.append(f"{ts_col}=NOW()")
        if to_state == "completed":
            sets.append("result=%s")
            vals.append(json.dumps(body.get("result")))
        if to_state == "failed" and body.get("reason"):
            sets.append("fail_reason=%s")
            vals.append(body["reason"])
        vals.append(task_id)
        cur.execute(
            f"UPDATE a2a_messages SET {', '.join(sets)} "
            f"WHERE task_id=%s AND parent_task_id IS NULL",
            vals,
        )
        c.commit()

    return {"ok": True, "task_id": task_id, "from": cur_state, "to": to_state}


# ─── Stale-task listing (P0.6 watchdog feed) ────────────────────────────
@app.get("/tasks/stale")
def stale_tasks():
    """Tasks the watchdog should act on. Designed for a curl+jq one-liner:

        curl -s localhost:9091/tasks/stale | jq -r '.tasks[].task_id'

    Returns root tasks that are:
      - `submitted` for > STALE_SUBMITTED_SECS without reaching `working`, or
      - `input-required` for > STALE_INPUT_REQ_SECS (escalate to the Boss).
    `age_secs` is from the row's last update; `reason` says which rule fired."""
    sql = """
        SELECT task_id, from_agent, to_agent, state, skill,
               bounce_count,
               EXTRACT(EPOCH FROM (NOW() - updated_at))::bigint AS age_secs
        FROM a2a_messages
        WHERE parent_task_id IS NULL
          AND (
                (state = 'submitted'
                   AND working_at IS NULL
                   AND NOW() - updated_at > make_interval(secs => %s))
             OR (state = 'input-required'
                   AND NOW() - updated_at > make_interval(secs => %s))
          )
        ORDER BY age_secs DESC
    """
    with db_conn() as c, c.cursor() as cur:
        cur.execute(sql, [STALE_SUBMITTED_SECS, STALE_INPUT_REQ_SECS])
        rows = cur.fetchall()
    for r in rows:
        if r["state"] == "submitted":
            r["reason"] = "submitted_no_working"
            r["action"] = "renotify"
        else:
            r["reason"] = "input_required_overdue"
            r["action"] = "escalate_logan"
    return {
        "ok": True,
        "count": len(rows),
        "thresholds": {
            "submitted_secs": STALE_SUBMITTED_SECS,
            "input_required_secs": STALE_INPUT_REQ_SECS,
        },
        "tasks": rows,
    }


@app.post("/{agent}/")
@app.post("/{agent}")
async def message_send(agent: str, request: Request):
    """Handle A2A JSON-RPC method=message/send."""
    if agent not in AGENT_CARDS:
        raise HTTPException(404, f"unknown agent: {agent}")

    payload = await request.json()
    method = payload.get("method")
    if method != "message/send":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": payload.get("id"),
             "error": {"code": -32601, "message": f"unsupported method: {method}"}},
            status_code=400,
        )

    params = payload.get("params") or {}
    msg = params.get("message") or {}
    parts = msg.get("parts") or []
    text = "\n".join(p.get("text", "") for p in parts if p.get("kind") == "text")
    task_id = msg.get("taskId") or params.get("taskId") or str(uuid.uuid4())
    from_agent = request.headers.get("x-blimp-from", "external")

    body = {
        "jsonrpc": "2.0",
        "method": "message/send",
        "params": {
            "message": {
                "role": msg.get("role", "user"),
                "parts": [{"kind": "text", "text": text}],
                "messageId": msg.get("messageId", str(uuid.uuid4())),
            },
        },
    }
    row_id = insert_message(
        task_id=task_id, parent_task_id=None,
        from_agent=from_agent, to_agent=agent,
        state="submitted", body=body,
    )

    # ── Mailroom: presence-gated doorbell (never inject into a busy session) ──
    delivered = notify_agent(agent, text, task_id)
    try:
        with db_conn() as c, c.cursor() as cur:
            if delivered:
                cur.execute("UPDATE a2a_messages SET notified_at=NOW(), attempt_count=1 WHERE id=%s", [row_id])
            else:
                cur.execute("UPDATE a2a_messages SET next_attempt_at=NOW()+INTERVAL '10 seconds' WHERE id=%s", [row_id])
            c.commit()
    except Exception as e:
        log.warning("send-stamp failed (task=%s): %s", task_id, e)

    # A2A response — TRUTHFUL receipt #1 (queued; delivery is confirmed async, not assumed)
    state_msg = (f"queued + notified {agent}; awaiting receipt (task_id={task_id})"
                 if delivered else
                 f"queued for {agent} (busy/offline — will deliver when free); task_id={task_id}")
    return {
        "jsonrpc": "2.0",
        "id": payload.get("id"),
        "result": {
            "id": task_id,
            "contextId": str(uuid.uuid4()),
            "kind": "task",
            "status": {"state": "submitted", "message": state_msg},
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
