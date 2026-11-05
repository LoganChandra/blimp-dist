"""
supervisor.py — Generator-Verifier orchestrator. Pure Python, no LangGraph.

Industry-standard PATTERN (Anthropic / OpenAI / LangGraph / MS all converged
on orchestrator-worker over peer-to-peer). LangGraph + a2a-sdk are the canonical
implementations, but both hit API churn on Python 3.14 — we implement the
pattern directly in ~200 lines instead of fighting the SDKs.

Flow:
  the Boss task → supervisor → Hermes (design) → Duke (verify)
    APPROVED            → END (final result)
    NEEDS_REVISION      → loop back to Hermes with Duke's feedback
    BLOCKED_NEEDS_LOGAN → END (escalation)
  Hard caps: turn count, USD budget.

HTTP API:
  POST /orchestrate {task, max_turns?, usd_budget?, requested_by?}
       → {thread_id, status: "running"}
  GET  /orchestrate/{thread_id} → run row
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import time
import uuid
from typing import Optional

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


DB_URL = os.environ["DUKE_DB_URL"]
A2A_BASE = os.environ.get("A2A_BROKER_PUBLIC_URL", "http://localhost:9091")
DEFAULT_MAX_TURNS = int(os.environ.get("ORCH_MAX_TURNS", "6"))
DEFAULT_USD_BUDGET = float(os.environ.get("ORCH_USD_BUDGET", "1.00"))
# Loopback by default (P0.2 security triage). Override with BIND_HOST (or the
# legacy ORCH_HOST) only for a Tailscale-interface bind — never 0.0.0.0.
HOST = os.environ.get("BIND_HOST") or os.environ.get("ORCH_HOST", "127.0.0.1")
PORT = int(os.environ.get("ORCH_PORT", "9092"))
POLL_TIMEOUT = int(os.environ.get("ORCH_POLL_TIMEOUT", "600"))
POLL_INTERVAL = int(os.environ.get("ORCH_POLL_INTERVAL", "3"))

# US$ per agent call (rough). Override via env if you tune.
COST_PER_CALL_USD = {
    "duke":   float(os.environ.get("DUKE_COST_PER_CALL", "0.10")),
    "hermes": float(os.environ.get("HERMES_COST_PER_CALL", "0.02")),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [supervisor] %(message)s")
log = logging.getLogger("supervisor")


def db_conn():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def insert_run(thread_id, task, requested_by, max_turns, usd_budget):
    with db_conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO orchestrator_runs
                (thread_id, task, pattern, status, max_turns, usd_budget)
            VALUES (%s,%s,'generator-verifier','running',%s,%s)
            ON CONFLICT (thread_id) DO NOTHING
        """, [thread_id, task, max_turns, usd_budget])
        c.commit()


def update_run(thread_id, **fields):
    if not fields: return
    cols = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = NOW()"
    with db_conn() as c, c.cursor() as cur:
        cur.execute(f"UPDATE orchestrator_runs SET {cols} WHERE thread_id = %s",
                    list(fields.values()) + [thread_id])
        c.commit()


def get_run(thread_id):
    with db_conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM orchestrator_runs WHERE thread_id = %s", [thread_id])
        return cur.fetchone()


def _post_a2a(to_agent, message, from_agent="supervisor", task_id=None):
    tid = task_id or str(uuid.uuid4())
    url = f"{A2A_BASE}/{to_agent}/"
    rpc = {
        "jsonrpc": "2.0", "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {"message": {
            "role": "user",
            "parts": [{"kind": "text", "text": message}],
            "messageId": str(uuid.uuid4()),
            "taskId": tid,
        }},
    }
    headers = {"x-blimp-from": from_agent, "Content-Type": "application/json"}
    with httpx.Client(timeout=30) as hx:
        r = hx.post(url, json=rpc, headers=headers)
        r.raise_for_status()
    return tid


def _poll_for_reply(task_id, to_supervisor="supervisor"):
    """Poll Postgres for a reply to task_id addressed back to us."""
    deadline = time.time() + POLL_TIMEOUT
    last_id = 0
    while time.time() < deadline:
        with db_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT id, body FROM a2a_messages
                WHERE parent_task_id = %s AND to_agent = %s AND id > %s
                ORDER BY id ASC LIMIT 1
            """, [task_id, to_supervisor, last_id])
            row = cur.fetchone()
        if row:
            body = row["body"]
            if isinstance(body, str):
                body = json.loads(body)
            try:
                parts = body["params"]["message"]["parts"]
                return "\n".join(p.get("text", "") for p in parts if p.get("kind") == "text")
            except Exception:
                return json.dumps(body)
        time.sleep(POLL_INTERVAL)
    return None


def call_agent(agent, prompt):
    task_id = _post_a2a(to_agent=agent, message=prompt, from_agent="supervisor")
    log.info("[%s] sent A2A task_id=%s", agent, task_id)
    reply = _poll_for_reply(task_id, to_supervisor="supervisor")
    cost = COST_PER_CALL_USD.get(agent, 0.05)
    if reply is None:
        return (f"[{agent}] TIMEOUT after {POLL_TIMEOUT}s", cost)
    return (reply, cost)


# ─── Generator-Verifier loop ─────────────────────────────────────────
def run_loop(thread_id, task, max_turns, usd_budget):
    """Synchronous orchestration loop. Called via asyncio.to_thread from the API."""
    design = None
    feedback = None
    verdict = None
    turn = 0
    usd = 0.0

    while turn < max_turns and usd < usd_budget:
        # Hermes: design (or revise)
        design_prompt = f"""You are Hermes, Blimp's Technical Lead. Design the requested deliverable.

TASK: {task}

{('REVISIONS REQUESTED BY DUKE (Implementation Lead):' + chr(10) + feedback) if feedback else ''}

Respond with your technical design. Be specific; state assumptions.
If you cannot proceed (missing info, blocked), start your reply with: BLOCKED:"""
        design, cost = call_agent("hermes", design_prompt)
        turn += 1
        usd += cost
        update_run(thread_id, turn_count=turn, usd_spent=usd)

        if design.strip().startswith("BLOCKED"):
            verdict = "BLOCKED_NEEDS_LOGAN"
            break
        if usd >= usd_budget or turn >= max_turns:
            break

        # Duke: verify
        verify_prompt = f"""You are Duke, Blimp's Implementation Lead. Verify Hermes's design.

ORIGINAL TASK: {task}

HERMES'S DESIGN:
{design}

Decide ONE of:
- APPROVED — design is sound. Reply: "APPROVED" + 1-2 sentence ship summary.
- NEEDS_REVISION — issues exist. Reply: "NEEDS_REVISION" + specific feedback.
- BLOCKED_NEEDS_LOGAN — need the Boss. Reply: "BLOCKED_NEEDS_LOGAN" + the question.

Your reply MUST start with one of those exact tokens."""
        feedback, cost = call_agent("duke", verify_prompt)
        turn += 1
        usd += cost
        update_run(thread_id, turn_count=turn, usd_spent=usd)

        verdict = None
        for tok in ("APPROVED", "NEEDS_REVISION", "BLOCKED_NEEDS_LOGAN"):
            if feedback.strip().startswith(tok):
                verdict = tok
                break
        if verdict is None:
            verdict = "NEEDS_REVISION"
            feedback = "NEEDS_REVISION (Duke didn't return a verdict token):\n" + feedback

        if verdict in ("APPROVED", "BLOCKED_NEEDS_LOGAN"):
            break

    # Finalize
    if usd >= usd_budget:
        status, result = "budget_exceeded", (
            f"⚠️ Budget exceeded (spent ~${usd:.2f} of ${usd_budget:.2f}).\n"
            f"Verdict at stop: {verdict}\n\nLast design:\n{design or '(none)'}\n\n"
            f"Last feedback:\n{feedback or '(none)'}"
        )
    elif turn >= max_turns and verdict not in ("APPROVED", "BLOCKED_NEEDS_LOGAN"):
        status, result = "max_turns_exceeded", (
            f"⚠️ Hit max_turns ({max_turns}). Verdict: {verdict}\n\n"
            f"Last design:\n{design or '(none)'}\n\nLast feedback:\n{feedback or '(none)'}"
        )
    elif verdict == "APPROVED":
        status, result = "completed", (
            f"✅ APPROVED.\n\nDuke's verdict:\n{feedback}\n\n---\nFinal design:\n{design}"
        )
    elif verdict == "BLOCKED_NEEDS_LOGAN":
        status, result = "blocked", f"🛑 BLOCKED — needs the Boss.\n\n{feedback or design}"
    else:
        status, result = "failed", f"Loop ended unexpectedly. Verdict: {verdict}"

    update_run(thread_id, status=status, final_result=result)
    with db_conn() as c, c.cursor() as cur:
        cur.execute("UPDATE orchestrator_runs SET completed_at = NOW() WHERE thread_id = %s",
                    [thread_id])
        c.commit()
    log.info("[%s] DONE status=%s turns=%d usd=$%.4f", thread_id, status, turn, usd)


# ─── HTTP API ────────────────────────────────────────────────────────
api = FastAPI(title="Blimp Orchestrator")


class OrchestrateRequest(BaseModel):
    task: str
    requested_by: str = "logan"
    max_turns: int = DEFAULT_MAX_TURNS
    usd_budget: float = DEFAULT_USD_BUDGET


@api.post("/orchestrate")
async def orchestrate(req: OrchestrateRequest):
    thread_id = str(uuid.uuid4())
    insert_run(thread_id, req.task, req.requested_by, req.max_turns, req.usd_budget)
    log.info("[%s] kicked off: %s (max_turns=%d budget=$%.2f)",
             thread_id, req.task[:80], req.max_turns, req.usd_budget)

    async def runner():
        try:
            await asyncio.to_thread(run_loop, thread_id, req.task,
                                     req.max_turns, req.usd_budget)
        except Exception as e:
            log.exception("orchestration error %s", thread_id)
            update_run(thread_id, status="failed", final_result=f"Exception: {e}")

    asyncio.create_task(runner())
    return {"thread_id": thread_id, "status": "running"}


@api.get("/orchestrate/{thread_id}")
def get_status(thread_id: str):
    row = get_run(thread_id)
    if not row:
        raise HTTPException(404, "thread not found")
    return row


@api.get("/healthz")
def healthz():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(api, host=HOST, port=PORT, log_level="info")
