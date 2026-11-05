"""Agent host for Duke and Frank (spec §10.3 turn lifecycle, §10.4 hosts; D-010: on the Boss's Claude
plan through Claude Code headless, not the API-billed Agent SDK).

One long-lived process per agent (systemd). For each turn request it:
  1. claims the request (dispatcher) and heartbeats it while the turn runs
  2. opens a Toolgate turn (bound to the task's live lease, or a task-less chat turn)
  3. compiles the context pack (L0..L7, cache-ordered) and records the load manifest
  4. runs `claude -p` with the pack as system prompt, the Toolgate MCP server as the ONLY tool
     source (--strict-mcp-config, no built-in tools), and the task's session resumed
  5. records usage, enforces Rule #0 (a the Boss-initiated turn that did not reply on Discord gets
     an automatic "on it" post and a rule0.miss event), flags a task turn without a ledger update,
     and finishes the request; a failure goes back to the queue (poison after 3)

Zero LLM in this file: the model runs inside the claude subprocess only.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from blimp.mesh.context.compiler import compile_pack, pack_hash  # noqa: E402
from blimp.mesh.deck.board import Board  # noqa: E402
from blimp.mesh.deck.cards import status_card, task_card  # noqa: E402
from blimp.mesh.deck.dispatch import Dispatcher  # noqa: E402
from blimp.mesh.deck.events import append_event  # noqa: E402
from blimp.mesh.toolgate.gate import Toolgate  # noqa: E402

MESH = Path(__file__).resolve().parents[1]
PROMPTS = MESH / "prompts"
MYT = timezone(timedelta(hours=8), "MYT")
DISPLAY = {"duke": "Duke", "frank": "Frank", "hermes": "Hermes"}
WALL_CLOCK_S = {"P0": 1800, "P1": 1800, "P2": 1800, "P3": 1200}   # spec 14.5 per_turn.max_wall_clock_s
HEARTBEAT_S = 30
PROMPT_VERSION = "2.0.0"

Runner = Callable[[dict[str, Any]], dict[str, Any]]


def load_layers(agent: str) -> dict[str, str]:
    def read(rel: str) -> str:
        p = PROMPTS / rel
        return p.read_text() if p.exists() else ""
    return {"constitution": read("L0/constitution.xml"), "identity": read(f"L1/{agent}.xml"),
            "protocol": read("L2/protocol.xml"), "playbook": skills_index()}


def skills_index() -> str:
    """L3 (spec 12.6): names + one line only; bodies are loaded just in time."""
    root = MESH.parents[1] / ".claude" / "skills"
    lines = []
    for skill in sorted(root.glob("*/SKILL.md")) if root.exists() else []:
        desc = ""
        for ln in skill.read_text(errors="replace").splitlines()[:15]:
            if ln.lower().startswith("description:"):
                desc = ln.split(":", 1)[1].strip()[:140]
                break
        lines.append(f"- {skill.parent.name} — {desc}")
    return "<skills_index>\n" + "\n".join(lines[:60]) + "\n</skills_index>" if lines else ""


def claude_runner(agent: str, model: str | None = None, claude: str = "claude") -> Runner:
    """Runs one turn through Claude Code headless. The only tools are Toolgate's (over MCP)."""
    def run(req: dict[str, Any]) -> dict[str, Any]:
        cfg = {"mcpServers": {"toolgate": {
            "command": sys.executable, "args": ["-m", "blimp.mesh.toolgate.mcp_server"],
            "env": {"BLIMPG_DSN": req["dsn"], "BLIMP_TURN_ID": req["turn_id"], "BLIMP_AGENT": agent,
                    "BLIMP_SANDBOX": os.environ.get("BLIMP_SANDBOX", "docker"), "PYTHONPATH": str(MESH.parents[1])}}}}
        with tempfile.TemporaryDirectory(prefix="blimp-turn-") as d:
            cfg_path = Path(d) / "mcp.json"
            cfg_path.write_text(json.dumps(cfg))
            sys_path = Path(d) / "pack.txt"
            sys_path.write_text(req["system_prompt"])
            argv = [claude, "-p", req["user_prompt"], "--output-format", "json",
                    "--append-system-prompt-file", str(sys_path),
                    "--strict-mcp-config", "--mcp-config", str(cfg_path),
                    "--tools", "", "--allowedTools", "mcp__toolgate",
                    "--setting-sources", "", "--permission-mode", "dontAsk"]
            if model:
                argv += ["--model", model]
            argv += (["--resume", req["session_id"]] if req.get("resume") else ["--session-id", req["session_id"]])
            # A STABLE, empty per-agent cwd: Claude Code files sessions by cwd, so --resume needs the
            # same directory every turn (a fresh temp dir made every resume fail). No CLAUDE.md here.
            home = Path(os.environ.get("BLIMP_HOST_CWD", os.path.expanduser("~/blimp/host-cwd"))) / agent
            home.mkdir(parents=True, exist_ok=True)
            p = subprocess.run(argv, capture_output=True, text=True, timeout=req["wall_clock_s"], cwd=str(home))
        try:
            out = json.loads(p.stdout)
        except ValueError:
            return {"ok": False, "error": f"claude exited {p.returncode}: {(p.stderr or p.stdout)[-400:]}"}
        u = out.get("usage") or {}
        return {"ok": not out.get("is_error") and p.returncode == 0, "session_id": out.get("session_id"), "result": out.get("result"),
                "error": None if not out.get("is_error") else str(out.get("result"))[:400],
                "usage": {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
                          "cache_read_tokens": u.get("cache_read_input_tokens", 0),
                          "cache_write_tokens": u.get("cache_creation_input_tokens", 0)},
                "list_price_usd": out.get("total_cost_usd"), "num_turns": out.get("num_turns"), "model": model or "default"}
    return run


class Host:
    def __init__(self, agent: str, dsn: str | None = None, runner: Runner | None = None, model: str | None = None):
        self.agent = agent
        self.dsn = dsn or os.environ.get("BLIMPG_DSN", "postgresql://logan@/duke_data?port=5433")
        self.board = Board(self.dsn)
        self.dispatch = Dispatcher(self.dsn)
        self.gate = Toolgate(self.dsn)
        self.runner = runner or claude_runner(agent, model)
        self.model = model or "default"
        self.host_id = f"{socket.gethostname()}:{os.getpid()}:{agent}"

    # ── context ──────────────────────────────────────────────────────────────
    def _trigger_batch(self, event_ids: list[int]) -> tuple[str, list[str], bool]:
        """What caused this turn, plus every unread message for this agent (teammates' messages
        that did not wake it, D-008), as data. Returns (text, message ids included, logan_wrote)."""
        lines, mids, logan = [], [], False
        seen: set[str] = set()
        with psycopg.connect(self.dsn) as c:
            rows = c.execute("SELECT id, type, actor, payload FROM mesh.events WHERE id = ANY(%s) ORDER BY id", (event_ids,)).fetchall()
        for eid, etype, actor, payload in rows:
            if etype == "message.sent":
                m = payload.get("message") or {}
                sa = (m.get("metadata") or {}).get("https://blimp.my/a2a/ext/speech-act/v1") or {}
                text = " ".join(pt.get("text", "") for pt in m.get("parts") or [] if isinstance(pt, dict))
                who = "the Boss" if actor == "logan" else DISPLAY.get(actor, actor)
                lines.append(f"[{sa.get('act')}] from {who} (message {m.get('messageId')}, task {m.get('taskId')}): {text}")
                mids.append(m.get("messageId"))
                seen.add(m.get("messageId"))
                logan = logan or actor == "logan"
            else:
                lines.append(f"[{etype}] by {actor}: {json.dumps(payload, default=str)[:600]}")
        unread = [m for m in self.board.inbox(self.agent, unread_only=True, limit=25) if m["id"] not in seen]
        if unread:
            lines.append("Unread messages (delivered while you were not running):")
        for m in unread:
            text = " ".join(pt.get("text", "") for pt in m["parts"] or [] if isinstance(pt, dict))
            lines.append(f"[{m['act']}] from {DISPLAY.get(m['from'], m['from'])} (message {m['id']}, task {m['task_id']}): {text[:800]}")
            mids.append(m["id"])
        return "\n".join(lines), [m for m in mids if m], logan

    def _pack(self, req: dict[str, Any], task: dict | None, trigger: str) -> Any:
        tail = ""
        if task:
            with psycopg.connect(self.dsn) as c:
                rows = c.execute("SELECT sender, act, purpose, parts, created_at FROM mesh.messages WHERE context_id=%s "
                                 "ORDER BY created_at DESC LIMIT 12", (task["context_id"],)).fetchall()
            tail = "\n".join(f"{r[4]:%d %b %H:%M} {r[0]} [{r[1]}] {r[2]}: " +
                             " ".join(p.get("text", "") for p in r[3] or [] if isinstance(p, dict))[:500] for r in reversed(rows))
        pages = {"now": status_card(self.board.list_tasks(limit=200))}
        if task:
            pages["task"] = task_card(task, self.board.ledger(task["id"]))
        return compile_pack(load_layers(self.agent), knowledge_pages=pages,
                            task_contract=json.dumps(task["contract"], indent=1, default=str) if task else "",
                            ledger=json.dumps(self.board.ledger(task["id"]), indent=1, default=str) if task else "",
                            thread_tail=tail, trigger_batch=trigger)

    # ── one turn ─────────────────────────────────────────────────────────────
    def run_once(self) -> dict[str, Any] | None:
        req = self.dispatch.claim(self.agent, self.host_id)
        if not req:
            return None
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_S):
                self.dispatch.beat(req["id"])
        threading.Thread(target=beat, daemon=True).start()
        turn_id = None
        try:
            task = self.board.get_task(req["task_id"]) if req["task_id"] else None
            if task and not (task["state"] == "WORKING" and (task["lease_holder"] or "").split(":")[0] == self.agent):
                task = None  # not holding it: a chat turn (the agent can claim through the board tools)
            turn_id = self.gate.open_turn(self.agent, task["id"] if task else None)
            turn_key = secrets.token_urlsafe(24)  # per-turn secret for runtimes that outlive a turn (Hermes)
            with psycopg.connect(self.dsn) as c:
                c.execute("UPDATE mesh.turn_requests SET turn_id=%s, turn_key_hash=%s WHERE id=%s",
                          (turn_id, hashlib.sha256(turn_key.encode()).hexdigest(), req["id"]))
            trigger, mids, logan_wrote = self._trigger_batch(list(req["trigger_event_ids"] or []))
            pack = self._pack(req, task, trigger)
            if mids:
                self.board.mark_read(mids, self.agent)  # read receipts come from code: included in the pack
            session_key = task["id"] if task else "chat"
            with psycopg.connect(self.dsn) as c:
                row = c.execute("SELECT session_id FROM mesh.host_sessions WHERE agent=%s AND task_key=%s", (self.agent, session_key)).fetchone()
            session_id, resume = (row[0], True) if row else (str(uuid.uuid4()), False)
            started = datetime.now(timezone.utc)
            with psycopg.connect(self.dsn) as c, c.cursor() as cur:
                cur.execute("""INSERT INTO mesh.turns (id, agent, task_id, trigger_event_ids, priority, model, prompt_manifest,
                               context_pack_sha256, started_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (turn_id, self.agent, task["id"] if task else None, list(req["trigger_event_ids"] or []), req["priority"],
                             self.model, Jsonb(pack.manifest), bytes.fromhex(pack_hash(pack)), started))
                append_event(cur, self.agent, "turn.started", {"turn_id": turn_id, "request_id": req["id"], "manifest": pack.manifest,
                                                               "prompt_version": PROMPT_VERSION, "session_id": session_id},
                             "turn", turn_id, correlation_id=task["id"] if task else None)
            user_prompt = ("Handle the <L7> trigger batch now. Use only the toolgate tools. "
                           + ("the Boss wrote to you: reply to him with comms_reply_discord in this turn. " if logan_wrote else "")
                           + ("Finish by updating the task ledger with board_ledger_update." if task else ""))
            out = self.runner({"dsn": self.dsn, "turn_id": turn_id, "system_prompt": pack.prompt_text, "user_prompt": user_prompt,
                               "session_id": session_id, "resume": resume, "wall_clock_s": WALL_CLOCK_S[req["priority"]],
                               "task_id": task["id"] if task else None, "turn_key": turn_key})
            self._after(req, turn_id, task, logan_wrote, session_key, out, started)
            self.dispatch.finish(req["id"], ok=bool(out.get("ok")), turn_id=turn_id, error=out.get("error"),
                                 retry=not self._had_effects(turn_id))
            return {"turn_id": turn_id, **out}
        except subprocess.TimeoutExpired:
            self._fail(turn_id, "wall clock limit reached")
            self.dispatch.finish(req["id"], ok=False, turn_id=turn_id, error="wall clock limit", retry=not self._had_effects(turn_id))
            return {"turn_id": turn_id, "ok": False, "error": "timeout"}
        except Exception as e:
            self._fail(turn_id, f"{type(e).__name__}: {e}")
            self.dispatch.finish(req["id"], ok=False, turn_id=turn_id, error=f"{type(e).__name__}: {e}", retry=not self._had_effects(turn_id))
            return {"turn_id": turn_id, "ok": False, "error": str(e)}
        finally:
            stop.set()

    def _had_effects(self, turn_id: str | None) -> bool:
        """Did this turn already change anything shared (an ok R2+ receipt, or a reply to the Boss)?
        Then a retry would repeat it: spec 10.5 "no work is repeated past the last receipt"."""
        if not turn_id:
            return False
        with psycopg.connect(self.dsn) as c:
            return c.execute("""SELECT count(*) FROM mesh.events e JOIN mesh.receipts r ON r.id = e.subject_id
                                WHERE e.type='tool.called' AND e.payload->>'turn_id'=%s AND r.status='ok'
                                AND r.risk_tier IN ('R2','R3')""", (turn_id,)).fetchone()[0] > 0

    def _receipt_count(self, turn_id: str, tool: str) -> int:
        with psycopg.connect(self.dsn) as c:
            # receipts carry the turn in their tool.called event
            return c.execute("""SELECT count(*) FROM mesh.events e JOIN mesh.receipts r ON r.id = e.subject_id
                                WHERE e.type='tool.called' AND e.payload->>'turn_id'=%s AND r.tool=%s AND r.status='ok'""",
                             (turn_id, tool)).fetchone()[0]

    def _after(self, req, turn_id, task, logan_wrote, session_key, out, started) -> None:
        u = out.get("usage") or {}
        replied = self._receipt_count(turn_id, "comms.reply_discord") > 0
        ledger_written = self._receipt_count(turn_id, "board.ledger_update") > 0
        with psycopg.connect(self.dsn) as c, c.cursor() as cur:
            cur.execute("""UPDATE mesh.turns SET input_tokens=%s, output_tokens=%s, cache_read_tokens=%s, cache_write_tokens=%s,
                           cost_usd=0, tool_calls=%s, outcome=%s, ended_at=now() WHERE id=%s""",
                        (u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_tokens"), u.get("cache_write_tokens"),
                         out.get("num_turns"), "completed" if out.get("ok") else "error", turn_id))
            if out.get("session_id"):
                cur.execute("""INSERT INTO mesh.host_sessions (agent, task_key, session_id, turns) VALUES (%s,%s,%s,1)
                               ON CONFLICT (agent, task_key) DO UPDATE SET session_id=EXCLUDED.session_id,
                               turns=mesh.host_sessions.turns+1, updated_at=now()""", (self.agent, session_key, out["session_id"]))
            if req["initiator"] == "logan" and logan_wrote and not replied:
                # Rule #0 (spec 10.3), enforced by the kernel, not by hooks.
                eta = (datetime.now(MYT) + timedelta(minutes=30)).strftime("%H:%M")
                text = f"{DISPLAY.get(self.agent, self.agent)} is on it: {task['id'] if task else 'your message'}. Update by {eta} MYT."
                cur.execute("""INSERT INTO mesh.outbox (idempotency_key, op, channel, persona, content)
                               VALUES (%s,'post',%s,%s,%s) ON CONFLICT DO NOTHING""",
                            (f"rule0:req:{req['id']}", f"dm:{self.agent}", self.agent, text))  # once per request, not per attempt
                append_event(cur, self.agent, "rule0.miss", {"turn_id": turn_id, "auto_ack": text}, "turn", turn_id)
            if task and not ledger_written:
                append_event(cur, self.agent, "turn.ledger_missing", {"turn_id": turn_id, "task_id": task["id"]}, "turn", turn_id,
                             correlation_id=task["id"])
            append_event(cur, self.agent, "turn.completed" if out.get("ok") else "turn.failed",
                         {"turn_id": turn_id, "usage": u, "list_price_usd": out.get("list_price_usd"), "replied": replied,
                          "ledger_written": ledger_written, "duration_s": int((datetime.now(timezone.utc) - started).total_seconds()),
                          "error": out.get("error")},
                         "turn", turn_id, correlation_id=task["id"] if task else None)

    def _fail(self, turn_id: str | None, error: str) -> None:
        if not turn_id:
            return
        with psycopg.connect(self.dsn) as c, c.cursor() as cur:
            cur.execute("UPDATE mesh.turns SET outcome='error', ended_at=now() WHERE id=%s", (turn_id,))
            append_event(cur, self.agent, "turn.failed", {"turn_id": turn_id, "error": error[:500]}, "turn", turn_id)

    # ── loop ─────────────────────────────────────────────────────────────────
    def serve(self, poll_s: float = 30.0) -> None:
        """LISTEN for new requests; a 30 s SQL poll is the fallback (spec 8.2). Zero tokens while idle."""
        conn = psycopg.connect(self.dsn, autocommit=True)
        conn.execute("LISTEN blimp_turns")
        while True:
            while self.run_once():
                pass
            for _ in conn.notifies(timeout=poll_s, stop_after=1):
                pass


def main() -> int:
    agent = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BLIMP_HOST_AGENT", "")
    if agent not in ("duke", "frank"):
        raise SystemExit("usage: python -m blimp.mesh.hosts.claude_host duke|frank")
    Host(agent, model=os.environ.get(f"BLIMP_MODEL_{agent.upper()}")).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
