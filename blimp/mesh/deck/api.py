"""Blimp Crew v2 — Deck kernel API (spec §10.1, §8.2): internal REST for hosts, Bridge and CLI,
plus the A2A v1.0 facade. ZERO LLM in this process: every decision here is code.

Security (spec §14.1 "identity spoofing between agents"): every request carries
`Authorization: Bearer <token>`; the token maps to exactly one principal. The token file
stores only sha256 hashes (`blimp.mesh.deck.api mint <principal>` prints a new token once).
A message's `from` must equal the authenticated principal. There is no endpoint that
appends a raw event: events are written only as side effects of board operations.

Bind to 127.0.0.1 (or the tailnet after the VPS move); never 0.0.0.0 (rail #5).
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from fastapi import Depends, FastAPI, Header, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from blimp.mesh.deck.board import A2A_STATE, Board, BoardError  # noqa: E402
from blimp.mesh.deck.cards import status_card, task_card  # noqa: E402
from blimp.mesh.deck.events import EventStore  # noqa: E402
from blimp.mesh.deck.router import WakeEvent, evaluate_wakes  # noqa: E402

DSN = os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")
TOKENS_FILE = Path(os.environ.get("DECK_TOKENS_FILE", os.path.expanduser("~/blimp/secrets/deck-tokens.json")))
SPEECH_ACT_EXT = "https://blimp.my/a2a/ext/speech-act/v1"

app = FastAPI(title="Blimp Deck", version="2.0.0")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_tokens() -> dict[str, str]:
    """sha256(token) -> principal. Missing file means no one can call the API (fail closed)."""
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def principal(authorization: str = Header(default="")) -> str:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authorization: Bearer <token> required")
    who = _load_tokens().get(_hash(authorization[7:].strip()))
    if not who:
        raise HTTPException(401, "unknown token")
    return who


@app.exception_handler(BoardError)
def _board_error(_: Request, e: BoardError):
    status = 404 if e.code == "task_not_found" else 403 if e.code.startswith("not_") else 409 if "transition" in e.code else 400
    return JSONResponse(status_code=status, content=e.as_dict())


def board() -> Board:
    return Board(DSN)


class NewTask(BaseModel):
    title: str
    type: str
    mandate_id: str | None = None
    objective_id: str | None = None
    owner: str | None = None


class Contract(BaseModel):
    contract: dict


class Move(BaseModel):
    to_state: str
    reason: str


class Ledger(BaseModel):
    ledger: dict


class MessageIn(BaseModel):
    message: dict


# ── health (no auth: reveals nothing but liveness and chain integrity) ─────────
@app.get("/health")
def health():
    ok, broken_at = EventStore(DSN).verify_chain()
    return {"status": "ok" if ok else "chain_broken", "chain_ok": ok, "first_broken_event": broken_at,
            "time": datetime.now(timezone.utc).isoformat()}


# ── board ────────────────────────────────────────────────────────────────────
@app.get("/tasks")
def list_tasks(owner: str | None = None, state: str | None = None, who: str = Depends(principal)):
    states = [s.strip().upper() for s in state.split(",")] if state else None
    return board().list_tasks(owner=owner, states=states)


@app.get("/tasks/{task_id}")
def get_task(task_id: str, who: str = Depends(principal)):
    b = board()
    return {"task": b.get_task(task_id), "ledger": b.ledger(task_id)}


@app.post("/tasks")
def create_task(body: NewTask, who: str = Depends(principal)):
    return board().create_task(title=body.title, type=body.type, created_by=who, owner=body.owner,
                               mandate_id=body.mandate_id, objective_id=body.objective_id)


@app.post("/tasks/{task_id}/propose")
def propose(task_id: str, body: Contract, who: str = Depends(principal)):
    return board().propose(task_id, body.contract, who)


@app.post("/tasks/{task_id}/accept")
def accept(task_id: str, reviewed_version: int, who: str = Depends(principal)):
    return board().make_ready(task_id, who, reviewed_version=reviewed_version)


@app.post("/tasks/{task_id}/renew")
def renew(task_id: str, who: str = Depends(principal)):
    return {"lease_expires_at": board().renew_lease(task_id, who)}


@app.post("/tasks/{task_id}/claim")
def claim(task_id: str, who: str = Depends(principal)):
    return board().claim(task_id, who)


@app.post("/tasks/{task_id}/move")
def move(task_id: str, body: Move, who: str = Depends(principal)):
    return board().transition(task_id, body.to_state.upper(), who, body.reason)


@app.post("/tasks/{task_id}/ledger")
def ledger(task_id: str, body: Ledger, who: str = Depends(principal)):
    return {"version": board().ledger_update(task_id, who, body.ledger)}


@app.get("/status")
def status(task: str | None = None, who: str = Depends(principal)):
    """Deterministic status card (spec 18.1 /status): no LLM."""
    b = board()
    if task:
        t = b.get_task(task)
        return {"text": task_card(t, b.ledger(task))}
    return {"text": status_card(b.list_tasks(limit=200))}


@app.get("/inbox")
def inbox(unread_only: bool = True, who: str = Depends(principal)):
    b = board()
    msgs = b.inbox(who, unread_only=unread_only)
    if msgs:
        b.mark_read([m["id"] for m in msgs], who)
    return msgs


# ── A2A v1.0 facade (spec 8.2): Deck is the A2A server for every agent ──────────
@app.post("/a2a/{agent}/message:send")
def a2a_send(agent: str, msg: MessageIn, who: str = Depends(principal), a2a_version: str = Header(default="")):
    if not a2a_version.startswith("1.0"):
        raise HTTPException(400, "A2A-Version: 1.0 required")
    sa = (msg.message.get("metadata") or {}).get(SPEECH_ACT_EXT) or {}
    if sa.get("from") != who:
        raise HTTPException(403, f"authenticated as {who!r} but message says from {sa.get('from')!r}")
    if agent not in (sa.get("to") or []):
        raise HTTPException(400, f"this endpoint is {agent}'s inbox; message.to must include {agent!r}")
    r = board().send(msg.message)
    if r.get("duplicate"):
        return {"messageId": r["id"], "duplicate": True, "wakes": []}
    ev = {"message": msg.message}
    wakes = evaluate_wakes([WakeEvent(r["event_id"], "message.sent", who, ev)])
    return {"messageId": r["id"], "taskId": r["task_id"], "warning": r.get("warning"),
            "wakes": [{"agent": w.agent, "priority": w.priority} for w in wakes]}


@app.get("/a2a/{agent}/tasks/{task_id}")
def a2a_task(agent: str, task_id: str, who: str = Depends(principal)) -> dict[str, Any]:
    """A2A Task view: A2A TaskState plus the Blimp substate in metadata (spec 9.4 mapping)."""
    t = board().get_task(task_id)
    return {"id": t["id"], "contextId": t["context_id"],
            "status": {"state": f"TASK_STATE_{A2A_STATE[t['state']]}"},
            "metadata": {"https://blimp.my/a2a/ext/contract/v1": {"blimp_state": t["state"], "substate": t.get("substate"),
                                                                "owner": t["owner"]}}}


@app.get("/verify-chain")
def verify_chain(who: str = Depends(principal)):
    ok, broken_at = EventStore(DSN).verify_chain()
    return {"ok": ok, "first_broken_event": broken_at}


# ── token minting (run by the Boss or Frank at the terminal; never over HTTP) ───────
def mint(principal_id: str) -> str:
    token = secrets.token_urlsafe(32)
    tokens = _load_tokens()
    tokens[_hash(token)] = principal_id
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2))
    os.chmod(TOKENS_FILE, 0o600)
    return token


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "mint":
        print(mint(sys.argv[2]))
    else:
        import uvicorn

        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DECK_PORT", "9100")))
