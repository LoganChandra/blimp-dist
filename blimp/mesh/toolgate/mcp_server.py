"""Toolgate as an MCP server (spec §13.1, §10.4): the ONLY tool surface an agent host exposes.

The host starts this process for one turn with its identity in the environment:
  BLIMP_TURN_ID  the gate turn (opened by the host from the board, bound to the lease holder)
  BLIMP_AGENT    the principal
The model only passes tool arguments; it cannot change who it is or which turn it is in.
Every call goes through Toolgate.call(), so authority, least privilege, taint, the Rule of Two,
approvals and receipts apply to all of them. Errors come back as results (spec 13.2).

Tool design (spec 13.2): one purpose each, named parameters (the first real run showed a
single JSON-blob argument made the model send empty or malformed calls), descriptions that say
what, when, and what comes back.

Run (stdio): python -m blimp.mesh.toolgate.mcp_server
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from blimp.mesh.toolgate.adapters import build  # noqa: E402
from blimp.mesh.toolgate.checks import CheckRunner  # noqa: E402
from blimp.mesh.toolgate.gate import Toolgate  # noqa: E402

EXT = "https://blimp.my/a2a/ext/speech-act/v1"

# MCP tool name -> catalog tool (spec 13.2: namespace.verb_object -> namespace_verb_object)
TOOLS = {
    "board_task_get": "board.task_get", "board_ledger_update": "board.ledger_update",
    "board_submit_completion": "board.submit_completion", "comms_send": "comms.send",
    "comms_reply_discord": "comms.reply_discord", "check_run": "check.run", "repo_read": "repo.read",
    "repo_grep": "repo.grep", "repo_run": "repo.run", "brain_recall": "brain.recall", "sys_whoami": "sys.whoami",
    "sys_request_approval": "sys.request_approval", "sys_turn_done": "sys.turn_done",
}


def make_server(dsn: str | None = None, turn_id: str | None = None, agent: str | None = None):
    from mcp.server.fastmcp import FastMCP

    dsn = dsn or os.environ.get("BLIMPG_DSN")
    agent = agent or os.environ["BLIMP_AGENT"]
    fixed_turn = turn_id or os.environ.get("BLIMP_TURN_ID")

    def current_turn(turn_key: str) -> str | None:
        """A per-turn process (Duke/Frank hosts) gets its turn in the env. A long-lived runtime with
        a static MCP config (the Hermes gateway) must present the per-turn key the kernel put in
        that turn's instruction; the call binds to the running request with that key's hash. A late
        call from an earlier turn, or a session the kernel did not start (e.g. her Discord chat),
        has no valid key and is refused. The model never names a turn."""
        if fixed_turn:
            return fixed_turn
        if not turn_key:
            return None
        import hashlib

        import psycopg

        with psycopg.connect(dsn) as c:
            row = c.execute("SELECT turn_id FROM mesh.turn_requests WHERE agent=%s AND status='running' AND turn_id IS NOT NULL "
                            "AND turn_key_hash=%s", (agent, hashlib.sha256(turn_key.encode()).hexdigest())).fetchone()
        return row[0] if row else None

    sandbox = None
    if os.environ.get("BLIMP_SANDBOX") == "docker":
        from blimp.mesh.toolgate.sandbox import DockerSandbox

        sandbox = DockerSandbox()
    runner = CheckRunner(dsn, sandbox=sandbox)
    gate = Toolgate(dsn, build(dsn, runner=runner, sandbox=sandbox))

    def request_approval(args, ctx):
        r = gate.request_approval(requested_by=ctx["agent"], executor="frank", tool=args.get("tool", ""),
                                  args=args.get("args") or {}, why=args.get("why", ""), rollback=args.get("rollback", ""),
                                  evidence=args.get("evidence") or [], task_id=ctx.get("task_id"))
        return r, f"approval {r['id']} requested", 0

    gate.adapters["sys.request_approval"] = request_approval
    mcp = FastMCP("blimp-toolgate")

    def go(tool: str, args: dict[str, Any], idempotency_key: str = "", approval_id: str = "", turn_key: str = "") -> str:
        tu = current_turn(turn_key)
        if tu is None:
            return json.dumps({"ok": False, "code": "no_turn", "reason": "no kernel turn is bound to this call (missing or stale turn_key)"})
        r = gate.call(tu, agent, tool, args, approval_id=approval_id or None, idempotency_key=idempotency_key or None)
        return json.dumps({"ok": r.allowed, "code": r.code, "reason": r.reason, "receipt_id": r.receipt_id, "result": r.result},
                          default=str)[:60_000]

    @mcp.tool()
    def board_task_get(task_id: str = "", turn_key: str = "") -> str:
        """Read a task: contract, state, lease, and its ledger. Empty task_id = your current task."""
        return go("board.task_get", {"task_id": task_id} if task_id else {}, turn_key=turn_key)

    @mcp.tool()
    def board_ledger_update(objective_restated: str, plan: list[dict], next_step: str, is_complete: bool = False,
                            is_progressing: bool = True, evidence: str = "", facts: list[dict] | None = None,
                            assumptions: list[dict] | None = None, decisions: list[str] | None = None,
                            blockers: list[str] | None = None, open_questions: list[dict] | None = None, turn_key: str = "") -> str:
        """Write your task's ledger: your resume point (spec 9.8). Call at the end of EVERY task turn.
        plan: list of {"step": 1, "what": "...", "status": "todo|doing|done|blocked|skipped", "receipts": ["RC-..."]}.
        is_looping is set by the system, not you."""
        ledger = {"objective_restated": objective_restated, "plan": plan, "next_step": next_step,
                  "progress": {"is_complete": is_complete, "is_progressing": is_progressing, "evidence": evidence},
                  "facts": facts or [], "assumptions": assumptions or [], "decisions": decisions or [],
                  "blockers": blockers or [], "open_questions": open_questions or []}
        return go("board.ledger_update", {"ledger": ledger}, turn_key=turn_key)

    @mcp.tool()
    def board_submit_completion(summary: str, claims: list[dict], checks: list[dict], deliverables: list[dict],
                                rollback: str, known_gaps: list[str] | None = None, lesson_candidate: str = "", turn_key: str = "") -> str:
        """Report DONE. claims: [{"text": "...", "receipts": ["RC-..."]}]; checks: [{"check_id": "c1",
        "receipt": "RC-...", "result": "pass"}] (receipts from check_run); deliverables: [{"name": "PR", "ref": "..."}].
        Rejected, with the exact problems, unless every claim and runnable check is backed by a valid receipt."""
        report = {"summary": summary, "claims": claims, "checks": checks, "deliverables": deliverables,
                  "rollback": rollback, "known_gaps": known_gaps or []}
        if lesson_candidate:
            report["lesson_candidate"] = lesson_candidate
        return go("board.submit_completion", {"report": report}, turn_key=turn_key)

    @mcp.tool()
    def comms_send(to: list[str], act: str, text: str, purpose: str, task_id: str, expects: str = "none",
                   reply_by: str = "", priority: str = "P2", in_reply_to: str = "", turn_key: str = "") -> str:
        """Send ONE typed message to teammates about a task. act: REQUEST QUERY ANSWER PROPOSE ACCEPT COUNTER REJECT
        PUSHBACK STATUS ALERT INFORM ESCALATE. First line of text states the intent. No "got it"/"thanks" (rejected).
        expects != none needs reply_by (ISO time). Over ~1,500 tokens: put it in an artifact and send the reference."""
        import secrets

        sa = {"act": act, "from": agent, "to": to, "expects": expects, "priority": priority, "purpose": purpose}
        if reply_by:
            sa["reply_by"] = reply_by
        if in_reply_to:
            sa["in_reply_to"] = in_reply_to
        m = {"messageId": secrets.token_hex(13), "contextId": f"ctx-{task_id}", "taskId": task_id, "role": "ROLE_AGENT",
             "parts": [{"text": text}], "metadata": {EXT: sa}}
        return go("comms.send", {"message": m}, turn_key=turn_key)

    @mcp.tool()
    def comms_reply_discord(text: str, in_thread: bool = False, turn_key: str = "") -> str:
        """Reply to the Boss on Discord: your own channel, or the task's thread with in_thread=true. When the Boss wrote
        to you, you MUST call this in the same turn. Phone-first: line 1 is the point, then up to 5 short bullets.
        No emojis. State only what receipts show."""
        return go("comms.reply_discord", {"text": text, "in_thread": in_thread}, turn_key=turn_key)

    @mcp.tool()
    def check_run(check_id: str, turn_key: str = "") -> str:
        """Run one acceptance check of your task by its id (the contract's own command and expected result, in the
        sandbox). Returns a receipt id; cite it in board_submit_completion. A failed check is ok=false."""
        return go("check.run", {"check_id": check_id}, turn_key=turn_key)

    @mcp.tool()
    def repo_read(path: str = ".", turn_key: str = "") -> str:
        """Read a file, or list a directory, inside your task's worktree (path relative to the worktree)."""
        return go("repo.read", {"path": path}, turn_key=turn_key)

    @mcp.tool()
    def repo_grep(pattern: str, turn_key: str = "") -> str:
        """Search your task's worktree (grep -rn). Returns matching lines with file:line."""
        return go("repo.grep", {"pattern": pattern}, turn_key=turn_key)

    @mcp.tool()
    def repo_run(cmd: str, turn_key: str = "") -> str:
        """Run a shell command in your task's sandbox: no network, only the worktree is visible and writable.
        Use it for builds and tests. Returns exit code, stdout and stderr."""
        return go("repo.run", {"cmd": cmd}, turn_key=turn_key)

    @mcp.tool()
    def brain_recall(query: str, limit: int = 5, turn_key: str = "") -> str:
        """Search the team brain (semantic). Results carry provenance and dates; old facts may be stale."""
        return go("brain.recall", {"query": query, "limit": limit}, turn_key=turn_key)

    @mcp.tool()
    def sys_turn_done(summary: str, turn_key: str = "") -> str:
        """Signal that you have finished this turn (runtimes that do not exit per turn). One line summary."""
        return go("sys.turn_done", {"summary": summary}, turn_key=turn_key)

    @mcp.tool()
    def sys_whoami(turn_key: str = "") -> str:
        """Your identity, task and turn, as the gate sees them."""
        return go("sys.whoami", {}, turn_key=turn_key)

    @mcp.tool()
    def sys_request_approval(tool: str, args: dict, why: str, rollback: str, evidence: list[str] | None = None, turn_key: str = "") -> str:
        """Ask the Boss to approve ONE exact R3 action (merge to main, prod deploy, client/public message, spending,
        secrets, DNS, restore). The token binds this tool + these exact args, is single-use, and Frank executes it."""
        return go("sys.request_approval", {"tool": tool, "args": args, "why": why, "rollback": rollback, "evidence": evidence or []}, turn_key=turn_key)

    return mcp


if __name__ == "__main__":
    make_server().run()
