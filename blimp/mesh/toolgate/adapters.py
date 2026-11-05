"""Toolgate adapters (spec §13.1 step 5): the code that actually performs each tool, called only
after the gate has authorized the call. Each returns (result, deterministic summary, exit_code).

Adapters never decide policy. They trust ctx (agent, task_id, turn_id), which comes from the
gate's turn record, never from the model's arguments.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..deck.board import Board, BoardError
from .checks import CheckRunner

REFUSED = 3  # exit code for "the tool refused the input" (validation): the caller's mistake, not an outage
AGENT_CHANNEL = {"duke": "dm:duke", "hermes": "dm:hermes", "frank": "dm:frank"}
MAX_READ = 60_000


def _board_err(fn):
    def wrapped(args, ctx):
        try:
            return fn(args, ctx)
        except BoardError as e:
            return e.as_dict(), f"refused: {e.code}: {e}" + (f" ({'; '.join(e.details)[:300]})" if e.details else ""), REFUSED
    return wrapped


def _worktree_path(board: Board, ctx: dict, rel: str) -> Path:
    """Resolve a path inside the task's worktree (roots-checked, per task); anything that escapes
    it, including through a symlink, is refused."""
    from .checks import resolve_worktree

    if not ctx.get("task_id"):
        raise PermissionError("repo tools need a task turn")
    root = Path(resolve_worktree(board.get_task(ctx["task_id"])["contract"] or {}, ctx["task_id"]))
    p = Path(os.path.realpath(root / (rel or ".")))
    if p != root and root not in p.parents:
        raise PermissionError(f"{rel!r} is outside the task worktree")
    return p


def build(dsn: str, runner: CheckRunner | None = None, sandbox=None) -> dict[str, Any]:
    board = Board(dsn)
    runner = runner or CheckRunner(dsn)
    a: dict[str, Any] = {}

    @_board_err
    def task_get(args, ctx):
        tid = args.get("task_id") or ctx.get("task_id")
        t = board.get_task(tid)
        return {"task": t, "ledger": board.ledger(tid)}, f"{tid} {t['state']}", 0

    @_board_err
    def ledger_update(args, ctx):
        v = board.ledger_update(ctx["task_id"], ctx["agent"], args.get("ledger") or {})
        return {"version": v}, f"ledger v{v}", 0

    @_board_err
    def submit_completion(args, ctx):
        r = board.submit_completion(ctx["task_id"], ctx["agent"], args.get("report") or {})
        return r, f"{ctx['task_id']} -> {r['state']}", 0

    @_board_err
    def comms_send(args, ctx):
        m = args.get("message") or {}
        sa = (m.get("metadata") or {}).get("https://blimp.my/a2a/ext/speech-act/v1") or {}
        if sa.get("from") != ctx["agent"]:
            return {"error": "from must be you"}, f"refused: message from {sa.get('from')!r} but you are {ctx['agent']}", REFUSED
        r = board.send(m)
        return r, f"sent {sa.get('act')} to {','.join(sa.get('to') or [])}", 0

    def reply_discord(args, ctx):
        """Queues a reply to the Boss in the agent's own channel (or the task thread) through the Bridge
        outbox. The receipt of this tool is what Rule #0 looks for (spec 10.3)."""
        import psycopg

        text = str(args.get("text") or "").strip()
        if not text:
            return {"error": "empty"}, "refused: empty reply (pass the message in `text`)", REFUSED
        where = "board" if args.get("in_thread") and ctx.get("task_id") else AGENT_CHANNEL.get(ctx["agent"], "hq")
        key = "reply:" + ctx["turn_id"] + ":" + hashlib.sha256(text.encode()).hexdigest()[:16]
        with psycopg.connect(dsn) as c:
            c.execute("""INSERT INTO mesh.outbox (idempotency_key, op, channel, context_id, persona, content)
                         VALUES (%s,'post',%s,%s,%s,%s) ON CONFLICT (idempotency_key) DO NOTHING""",
                      (key, where, f"ctx-{ctx['task_id']}" if where == "board" else None, ctx["agent"], text[:1900]))
        return {"queued": key, "channel": where}, f"reply queued for #{where}", 0

    def check_run(args, ctx):
        if not ctx.get("task_id"):
            return {"error": "no task"}, "refused: check.run needs a task turn", REFUSED
        return runner.run(ctx["task_id"], str(args.get("check_id", "")))

    def repo_read(args, ctx):
        p = _worktree_path(board, ctx, str(args.get("path", "")))
        if p.is_dir():
            items = sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir())[:500]
            return {"entries": items}, f"{len(items)} entries", 0
        data = p.read_text(errors="replace")
        return {"content": data[:MAX_READ], "truncated": len(data) > MAX_READ}, f"read {len(data)} chars", 0

    def repo_grep(args, ctx):
        root = _worktree_path(board, ctx, "")
        pat = str(args.get("pattern", ""))
        if not pat:
            return {"error": "pattern required"}, "refused: empty pattern", REFUSED
        r = subprocess.run(["grep", "-rnI", "--exclude-dir=.git", "-m", "200", "-e", pat, "."], cwd=root,
                           capture_output=True, text=True, timeout=60)
        return {"matches": r.stdout[:MAX_READ]}, f"{r.stdout.count(chr(10))} matching lines", 0 if r.returncode in (0, 1) else r.returncode

    def repo_run(args, ctx):
        """Runs a command for the agent in the task's sandbox (spec 10.6). Without a sandbox, refused:
        agent-chosen commands never run on the host."""
        if sandbox is None:
            return {"error": "no sandbox"}, "refused: repo.run needs the per-task sandbox (spec 10.6), which is not configured", 1
        root = _worktree_path(board, ctx, "")
        return sandbox.run(str(args.get("cmd", "")), workdir=str(root), task_id=ctx["task_id"])

    def brain_recall(args, ctx):
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "blimp-infra" / "postgres"))
        import hindsight_client as hc  # noqa: E402

        q = str(args.get("query", ""))
        hits = hc.recall(q, limit=min(int(args.get("limit", 5)), 10))
        return {"hits": hits}, f"{len(hits) if isinstance(hits, list) else 0} memories", 0

    def turn_done(args, ctx):
        import psycopg

        from ..deck.events import append_event

        with psycopg.connect(dsn) as c, c.cursor() as cur:
            append_event(cur, ctx["agent"], "turn.done_signal", {"turn_id": ctx["turn_id"], "summary": str(args.get("summary", ""))[:500]},
                         "turn", ctx["turn_id"], correlation_id=ctx.get("task_id"))
        return {"turn_id": ctx["turn_id"]}, "turn end signalled", 0

    def whoami(args, ctx):
        return dict(ctx), f"{ctx['agent']} turn {ctx['turn_id']}", 0

    a.update({"board.task_get": task_get, "board.ledger_update": ledger_update, "board.submit_completion": submit_completion,
              "comms.send": comms_send, "comms.reply_discord": reply_discord, "check.run": check_run,
              "repo.read": repo_read, "repo.grep": repo_grep, "repo.run": repo_run, "brain.recall": brain_recall,
              "sys.whoami": whoami, "sys.turn_done": turn_done})
    return a
