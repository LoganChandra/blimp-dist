#!/usr/bin/env python3
"""
engine.py — the brain-under-test interface for the benchmark harness.

Everything the harness does to the Blimp brain goes through here: spin up an
ISOLATED throwaway Hindsight bank, ingest a conversation's history into it,
run recall() against it exactly as Duke would, optionally generate an answer
from the recalled context, then tear the bank down. The live `duke` bank is
never touched.

SAFETY (non-negotiable): every bank this module creates, writes to, or deletes
must have a name starting with BENCH_PREFIX. `_guard()` refuses anything else,
so a bug can never ingest into or DELETE the real team brain (the 2026-06-23
wipe is why this guard is a hard assert, not a comment).

The harness reuses the real hindsight_client the agents use, so we measure the
ACTUAL recall path (hybrid + graph + temporal + rerank, provenance, MMR
diversity), not a reimplementation.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
import contextlib
from typing import Optional

import httpx

# Reach the real client the agents use (blimp-infra/postgres/hindsight_client.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "postgres"))
os.environ.setdefault("HINDSIGHT_URL", "http://localhost:8888")
import hindsight_client as hc  # noqa: E402

BENCH_PREFIX = "bench_"          # every throwaway bank name MUST start with this
LIVE_BANKS = {"duke"}            # names we must NEVER touch, belt-and-braces


def _guard(bank: str) -> str:
    """Refuse to operate on anything that isn't a clearly-throwaway bench bank."""
    if not bank or not bank.startswith(BENCH_PREFIX) or bank in LIVE_BANKS:
        raise RuntimeError(
            f"refusing to operate on non-bench bank {bank!r} — "
            f"harness banks must start with {BENCH_PREFIX!r}"
        )
    return bank


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    """Read a key from ~/blimp/.env then the process env (same source the
    judge + Hindsight use), so the harness needs no separate config."""
    try:
        for line in open(os.path.expanduser("~/blimp/.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(key, default)


def health() -> bool:
    return hc.health()


def _retry(fn, tries: int = 5, base: float = 2.0, what: str = "call"):
    """Retry with exponential backoff on transient provider errors — the brain's
    reranker/embeddings occasionally return 429/TooManyRequests, and a benchmark
    run makes many calls, so a naive run would die mid-way. Re-raises the last
    error after `tries`."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            msg = str(e).lower()
            transient = ("toomanyrequests" in msg or "429" in msg
                         or "rate" in msg or "timeout" in msg or "503" in msg
                         or "502" in msg or "temporarily" in msg)
            if not transient or i == tries - 1:
                raise
            time.sleep(base * (2 ** i))
    raise last  # unreachable


# ---------------------------------------------------------------------------
# Isolated bank lifecycle
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def isolated_bank(label: str = "run", keep: bool = False):
    """Context manager yielding a fresh, uniquely-named throwaway bank.

    On enter: create + init the bank and point the client at it.
    On exit: restore the client to the live bank and DELETE the throwaway
    (unless keep=True, for debugging a failed run).

    The client's module-global BANK_ID is what _bank_path() reads on every
    call, so we swap it here and ALWAYS restore it in the finally — the harness
    process must never be left pointing at a bench bank.
    """
    safe_label = "".join(c for c in label if c.isalnum() or c in "-_")[:32]
    bank = _guard(f"{BENCH_PREFIX}{safe_label}_{uuid.uuid4().hex[:10]}")
    prev = hc.BANK_ID
    hc.BANK_ID = bank
    try:
        hc.init_bank()
        yield bank
    finally:
        hc.BANK_ID = prev  # restore FIRST, so a delete failure can't strand us
        if not keep:
            try:
                delete_bank(bank)
            except Exception as e:
                print(f"  ! teardown: could not delete {bank}: {str(e)[:80]}",
                      file=sys.stderr)


def delete_bank(bank: str) -> int:
    """DELETE a throwaway bank. Guarded. Returns the HTTP status."""
    _guard(bank)
    url = hc.HINDSIGHT_URL + f"/v1/default/banks/{bank}"
    r = httpx.delete(url, timeout=60.0)
    return r.status_code


# ---------------------------------------------------------------------------
# Ingestion — write a conversation's turns into the current bench bank
# ---------------------------------------------------------------------------

def ingest_turns(bank: str, turns: list[dict], batch: int = 25) -> int:
    """Ingest conversation turns as episodic memories into `bank`.

    Each turn = {"speaker": str, "text": str, "timestamp": optional ISO str,
                 "session": optional session label}. We write them synchronously
    (async=False) so recall is meaningful the moment ingestion returns, and with
    dedup OFF (benchmark facts must never be merged with each other).

    Returns the number of memories posted.
    """
    _guard(bank)
    if hc.BANK_ID != bank:  # real raise, not assert — survives python -O
        raise RuntimeError(f"ingest target {hc.BANK_ID!r} != bench bank {bank!r}")
    items = []
    for t in turns:
        text = (t.get("text") or "").strip()
        if not text:
            continue
        speaker = t.get("speaker") or "user"
        sess = t.get("session")
        content = f"[{speaker}] {text}"
        meta = {"speaker": speaker, "kind": "chat"}
        if sess:
            meta["session"] = str(sess)
        item = hc._memory_item(
            content,
            tags=["conversation", f"speaker:{speaker}"],
            metadata=meta,
            timestamp=t.get("timestamp") or None,
        )
        items.append(item)

    posted = 0
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        _retry(lambda: hc._post(hc._bank_path("/memories"),
                                {"items": chunk, "async": False}),
               what="ingest")
        posted += len(chunk)
    return posted


def wait_until_indexed(bank: str, expect_min: int = 1,
                       timeout_s: float = 90.0, poll_s: float = 2.0) -> bool:
    """Block until the bank reports at least `expect_min` stored units (or
    timeout). Synchronous ingest usually means this returns immediately, but
    Hindsight chunks/embeds server-side, so we confirm before querying."""
    _guard(bank)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            s = hc.bank_stats()
            n = _stat_count(s)
            if n >= expect_min:
                return True
        except Exception:
            pass
        time.sleep(poll_s)
    return False


def _stat_count(stats: dict) -> int:
    """Bank stats shape varies by Hindsight version — dig out any memory count."""
    if not isinstance(stats, dict):
        return 0
    for k in ("memory_count", "memories", "num_memories", "count", "units",
              "unit_count", "total"):
        v = stats.get(k)
        if isinstance(v, int):
            return v
    # nested
    for key in ("stats", "data"):
        if isinstance(stats.get(key), dict):
            n = _stat_count(stats[key])
            if n:
                return n
    return 0


# ---------------------------------------------------------------------------
# Recall + answer generation (the thing we're scoring)
# ---------------------------------------------------------------------------

def recall(bank: str, question: str, k: int = 10) -> list[dict]:
    """Recall as Duke would — the real hybrid+graph+temporal+rerank path."""
    _guard(bank)
    if hc.BANK_ID != bank:  # real raise, not assert — survives python -O
        raise RuntimeError(f"recall target {hc.BANK_ID!r} != bench bank {bank!r}")
    # Patient retry: the reranker (Cohere) is easily rate-limited right after an
    # ingest burst; ride it out (4,8,16,32,64s ≈ 2min) rather than losing the Q.
    return _retry(lambda: hc.recall(question, limit=k, diversify=True),
                  tries=6, base=4, what="recall")


def context_text(rows: list[dict], max_chars: int = 8000) -> str:
    """Flatten recalled rows into a context block for the answerer/judge."""
    out, used = [], 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = (r.get("text") or r.get("content") or "").strip()
        if not t:
            continue
        if used + len(t) > max_chars:
            t = t[: max(0, max_chars - used)]
        out.append(t)
        used += len(t)
        if used >= max_chars:
            break
    return "\n".join(out)


_ANSWER_MODEL = _env("BRAIN_BENCH_ANSWER_MODEL",
                     _env("HINDSIGHT_LLM_MODEL", "google/gemini-2.5-flash"))
_OR_KEY = _env("OPENROUTER_API_KEY")


def generate_answer(question: str, context: str,
                    question_date: Optional[str] = None) -> str:
    """Answer the question using ONLY the recalled context — the end-to-end QA
    path published benchmarks score. If retrieval missed the needle, the model
    should say it doesn't know; that (correctly) counts against us."""
    if not _OR_KEY:
        raise RuntimeError("no OPENROUTER_API_KEY for answer generation")
    date_line = f"\nThe question is asked on: {question_date}\n" if question_date else ""
    prompt = (
        "You are answering a question using ONLY the retrieved memory below "
        "from an earlier conversation. If the memory does not contain the "
        "answer, say 'I don't know' — do not guess.\n"
        f"{date_line}"
        f"\nRetrieved memory:\n{context or '(nothing retrieved)'}\n\n"
        f"Question: {question}\n"
        "Answer concisely (a few words or one sentence):"
    )
    def _call():
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {_OR_KEY}", "Content-Type": "application/json"},
            json={"model": _ANSWER_MODEL, "temperature": 0, "max_tokens": 256,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    return _retry(_call, what="answer")
