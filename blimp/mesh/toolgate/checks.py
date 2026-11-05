"""Check runner (spec §9.11 "the check runner", §13.4 deterministic receipt summaries).

The `check.run` tool takes a CHECK ID, never a command: it reads the check from the task's
frozen contract, so an agent can neither run an arbitrary command under a "check" receipt
nor edit the expectation after the fact. The runner compares the output to the contract's
`expected` value deterministically and the receipt's summary is the runner's own text.

`expected` grammar (documented for contract authors):
  exit N                  process exit code equals N (test / cmd)
  status N  |  N          HTTP status equals N (http)
  contains: <text>        output / body contains <text>
  regex: <pattern>        output / body matches <pattern> (re.search, multiline)
  >= N  <= N  > N  < N  = N  != N
                          numeric comparison against the first output line / first SQL cell
  anything else           exact match with the trimmed output (first SQL cell as text)

Kinds manual_by_logan and review cannot run here: they are UNVERIFIABLE for the runner and go
to the Boss's decision window (spec 9.11 step 5).
"""
from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any

import psycopg

from ..deck.board import contract_sha
from ..deck.contract_lint import DEFAULT_HTTP_ALLOWLIST, _host_allowed, _sql_readonly

CMD_TIMEOUT_S = int(os.environ.get("BLIMP_CHECK_TIMEOUT_S", "600"))
TAIL = 1500
_NUM = re.compile(r"^\s*(>=|<=|!=|>|<|=)\s*(-?\d+(?:\.\d+)?)\s*$")


class CheckNotRunnable(Exception):
    pass


def compare(expected: str, *, output: str, exit_code: int | None = None, status: int | None = None) -> tuple[bool, str]:
    exp = (expected or "").strip()
    out = (output or "").strip()
    m = re.match(r"^exit\s+(-?\d+)$", exp, re.I)
    if m:
        want = int(m.group(1))
        return exit_code == want, f"exit {exit_code} (expected {want})"
    m = re.match(r"^(?:status\s+)?(\d{3})$", exp, re.I)
    if m and status is not None:
        want = int(m.group(1))
        return status == want, f"HTTP {status} (expected {want})"
    if exp.lower().startswith("contains:"):
        needle = exp.split(":", 1)[1].strip()
        return needle in out, f"output {'contains' if needle in out else 'does not contain'} {needle!r}"
    if exp.lower().startswith("regex:"):
        pat = exp.split(":", 1)[1].strip()
        hit = re.search(pat, out, re.M) is not None
        return hit, f"output {'matches' if hit else 'does not match'} /{pat}/"
    m = _NUM.match(exp)
    if m:
        op, want = m.group(1), float(m.group(2))
        first = out.splitlines()[0].strip() if out else ""
        try:
            got = float(first)
        except ValueError:
            return False, f"first output line {first!r} is not a number (expected {op} {m.group(2)})"
        ok = {">=": got >= want, "<=": got <= want, ">": got > want, "<": got < want, "=": got == want, "!=": got != want}[op]
        return ok, f"{first} {op} {m.group(2)}: {'yes' if ok else 'no'}"
    return out == exp, f"output {'equals' if out == exp else 'differs from'} expected"


def _roots() -> list[str]:
    raw = os.environ.get("BLIMP_WORKTREE_ROOTS", os.path.expanduser("~/blimp/worktrees"))
    return [os.path.realpath(os.path.expanduser(r)) for r in raw.split(":") if r.strip()]


def resolve_worktree(contract: dict, task_id: str, roots: list[str] | None = None) -> str:
    """The ONE rule for a task's worktree (checks, repo tools and the sandbox all use it):
    realpath strictly under an allowed root, in a directory named for the task
    (<root>/<task id>[-slug]/...), so no task can point at the home folder, a root itself,
    or another task's checkout. Symlinks are resolved before the check."""
    roots = roots if roots is not None else _roots()
    wt = contract.get("worktree")
    if not wt:
        raise PermissionError("the contract has no worktree")
    real = os.path.realpath(os.path.expanduser(wt))
    for r in roots:
        if real.startswith(r + os.sep):
            first = real[len(r) + 1:].split(os.sep, 1)[0]
            if first == task_id or first.startswith(task_id + "-"):
                if not os.path.isdir(real):
                    raise PermissionError(f"worktree {wt} does not exist")
                return real
            raise PermissionError(f"worktree {wt} must be under {r}/{task_id}... (one checkout per task)")
    raise PermissionError(f"worktree {wt} is outside the allowed roots {roots}")


class CheckRunner:
    """Safety rules (phase 2 security review, 2026-09-30):
    - test/cmd checks run a shell command, so they run only after someone OTHER than the
      contract's author approved this exact contract (board.approve_checks), and only inside the
      contract's `worktree`, which must be under BLIMP_WORKTREE_ROOTS (default ~/blimp/worktrees).
      Never in the process's own directory: the verifier must test the agent's change, not main.
    - sql_readonly checks run only on a dedicated read-only role (BLIMP_CHECK_SQL_DSN) that is not
      the gate's own login; a READ ONLY transaction alone does not stop a superuser reading files.
    """

    def __init__(self, dsn: str, http_allowlist=DEFAULT_HTTP_ALLOWLIST, opener=urllib.request.urlopen,
                 sql_dsn: str | None = None, worktree_roots: list[str] | None = None, sandbox=None):
        self.dsn = dsn
        if sandbox is None and os.environ.get("BLIMP_SANDBOX") == "docker":
            from .sandbox import DockerSandbox

            sandbox = DockerSandbox()
        self.sandbox = sandbox  # spec 10.6: when set, test/cmd checks run in the per-task container
        # Fail closed: without a sandbox, command checks run only if explicitly allowed (tests).
        self.host_exec_allowed = os.environ.get("BLIMP_SANDBOX") == "none"
        self.allow = http_allowlist
        self._open = opener
        self.sql_dsn = sql_dsn or os.environ.get("BLIMP_CHECK_SQL_DSN")
        self.roots = worktree_roots if worktree_roots is not None else _roots()

    def contract_check(self, task_id: str, check_id: str) -> dict[str, Any]:
        with psycopg.connect(self.dsn) as c:
            row = c.execute("SELECT contract, contract_frozen_at, contract_author, checks_approved_by, checks_approved_sha "
                            "FROM mesh.tasks WHERE id=%s", (task_id,)).fetchone()
        if not row:
            raise CheckNotRunnable(f"task {task_id} does not exist")
        contract, frozen, author, approver, approved_sha = row
        if not frozen:
            raise CheckNotRunnable(f"{task_id} has no frozen contract yet (checks run only after READY)")
        for chk in contract.get("acceptance_checks") or []:
            if chk.get("id") == check_id:
                if chk.get("kind") in ("test", "cmd"):
                    if not approver or approver == author or approved_sha != contract_sha(contract):
                        raise CheckNotRunnable(f"{check_id} runs a command: it needs approval of this exact contract by someone "
                                               f"other than its author ({author}) before it can run (board_approve_checks)")
                    chk = {**chk, "_worktree": self._worktree(contract, task_id)}
                return chk
        raise CheckNotRunnable(f"{task_id} has no acceptance check {check_id!r}")

    def _worktree(self, contract: dict, task_id: str) -> str:
        try:
            return resolve_worktree(contract, task_id, self.roots)
        except PermissionError as e:
            raise CheckNotRunnable(str(e))

    def run(self, task_id: str, check_id: str) -> tuple[dict[str, Any], str, int]:
        """Adapter shape for Toolgate: (result, summary, exit_code). exit 0 == the check PASSED."""
        chk = self.contract_check(task_id, check_id)
        kind, expected = chk.get("kind"), chk.get("expected", "")
        if kind in ("manual_by_logan", "review"):
            res = {"check_id": check_id, "kind": kind, "passed": None, "unverifiable": True, "expected": expected}
            return res, f"{check_id}: {kind} checks are not machine-runnable; routed to the Boss", 2
        if kind in ("test", "cmd"):
            if self.sandbox is not None:
                sres, _, rc = self.sandbox.run(chk["cmd"], workdir=chk["_worktree"], task_id=task_id)
                if rc == 124 and "timeout_s" in sres:
                    res = {"check_id": check_id, "kind": kind, "passed": False, "timeout_s": sres["timeout_s"], "expected": expected, "sandbox": True}
                    return res, f"{check_id}: timed out in the sandbox", 1
                stdout, stderr = sres.get("stdout", ""), sres.get("stderr", "")
            else:
                if not self.host_exec_allowed:
                    raise CheckNotRunnable(f"{check_id}: command checks need the sandbox (BLIMP_SANDBOX=docker); refusing to run on the host")
                try:
                    p = subprocess.run(chk["cmd"], shell=True, cwd=chk["_worktree"], capture_output=True, text=True, timeout=CMD_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    res = {"check_id": check_id, "kind": kind, "passed": False, "timeout_s": CMD_TIMEOUT_S, "expected": expected}
                    return res, f"{check_id}: timed out after {CMD_TIMEOUT_S}s", 1
                rc, stdout, stderr = p.returncode, p.stdout, p.stderr
            output = (stdout or "") + (("\n" + stderr) if stderr and not stdout else "")
            ok, why = compare(expected, output=output, exit_code=rc)
            last = next((ln for ln in reversed(output.strip().splitlines()) if ln.strip()), "")
            res = {"check_id": check_id, "kind": kind, "passed": ok, "exit_code": rc,
                   "output_tail": output[-TAIL:], "expected": expected, "sandbox": self.sandbox is not None}
            return res, f"{check_id}: {'PASS' if ok else 'FAIL'} — {why}; last line: {last[:200]}", 0 if ok else 1
        if kind == "http":
            bad = _host_allowed(chk["url"], self.allow)
            if bad:
                raise CheckNotRunnable(f"{check_id}: url {bad}")
            try:
                with self._open(urllib.request.Request(chk["url"], headers={"User-Agent": "BlimpCheckRunner/2.0"}), timeout=30) as r:
                    status, body = r.status, r.read(200_000).decode(errors="replace")
            except urllib.error.HTTPError as e:
                status, body = e.code, e.read(200_000).decode(errors="replace") if e.fp else ""
            ok, why = compare(expected, output=body, status=status)
            res = {"check_id": check_id, "kind": kind, "passed": ok, "status": status, "body_tail": body[-TAIL:], "expected": expected}
            return res, f"{check_id}: {'PASS' if ok else 'FAIL'} — {why}", 0 if ok else 1
        if kind == "sql_readonly":
            bad = _sql_readonly(chk["cmd"])
            if bad:
                raise CheckNotRunnable(f"{check_id}: sql {bad}")
            if not self.sql_dsn or self.sql_dsn == self.dsn:
                raise CheckNotRunnable(f"{check_id}: sql checks need BLIMP_CHECK_SQL_DSN, a dedicated read-only role (not the gate's login)")
            with psycopg.connect(self.sql_dsn) as c:
                c.execute("SET TRANSACTION READ ONLY")  # the database enforces it too
                c.execute("SET LOCAL statement_timeout = '30s'")
                row = c.execute(chk["cmd"]).fetchone()
            first = "" if not row else str(row[0])
            ok, why = compare(expected, output=first)
            res = {"check_id": check_id, "kind": kind, "passed": ok, "first_cell": first, "expected": expected}
            return res, f"{check_id}: {'PASS' if ok else 'FAIL'} — {why}", 0 if ok else 1
        raise CheckNotRunnable(f"{check_id}: unknown kind {kind!r}")

    def adapter(self):
        """Toolgate adapter: args {check_id}; the task comes from the turn, never from the caller."""
        def _run(args: dict[str, Any], ctx: dict[str, Any]):
            if not ctx.get("task_id"):
                raise CheckNotRunnable("check.run needs a task turn")
            return self.run(ctx["task_id"], str(args.get("check_id", "")))
        return _run
