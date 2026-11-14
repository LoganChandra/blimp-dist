"""idempotency.py: run a side-effecting tool once per key, even if the agent calls it twice.

Agents retry. A timeout, a context compaction or a resumed session can repeat the same send_discord,
a2a_send or board_post, and the Boss gets the message twice or a peer gets the task twice. With an
idempotency key the second call returns the first call's result instead of acting again (the
Stripe API pattern: stripe.com/docs/api/idempotent_requests).

Keys are per agent, kept 24 h in ~/.cache/blimp-mcp/idempotency-<agent>.json. Only successful
results are remembered, so a failed send can be retried with the same key. Stdlib only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

TTL = 24 * 3600
DIR = Path(os.getenv("BLIMP_IDEMPOTENCY_DIR", str(Path.home() / ".cache" / "blimp-mcp")))


@contextlib.contextmanager
def _locked(agent: str):
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / f"idempotency-{agent}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(path: Path, d: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".idem.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _ok(result: str) -> bool:
    try:
        r = json.loads(result)
    except Exception:
        return False
    return isinstance(r, dict) and r.get("ok", True) is not False and "error" not in r


def once(agent: str, tool: str, key: str | None, fn: Callable[[], str], args: object = None) -> str:
    """fn() unless this (tool, key) already succeeded in the last 24 h; then the stored result.

    args: what the call is about (recipient, message...). Reusing a key for DIFFERENT args is refused,
    never silently answered with the old result (Stripe does the same).
    """
    if not key:
        return fn()
    fp = hashlib.sha256(json.dumps(args, sort_keys=True, default=str).encode()).hexdigest()[:16]
    path = DIR / f"idempotency-{agent}.json"
    k = f"{tool}:{key}"
    with _locked(agent):
        now = time.time()
        store = {kk: v for kk, v in _load(path).items() if now - v.get("at", 0) < TTL}
        if k in store:
            if store[k].get("args") not in (None, fp):
                return json.dumps({"ok": False, "error": f"idempotency_key {key!r} was already used in the last "
                                   f"24 h for a different {tool}; use a new key for a new message"})
            r = json.loads(store[k]["result"])
            if isinstance(r, dict):
                r["idempotent_replay"] = True
                return json.dumps(r, default=str)
            return store[k]["result"]
        result = fn()
        if _ok(result):
            store[k] = {"at": now, "result": result, "args": fp}
        _save(path, store)
        return result
