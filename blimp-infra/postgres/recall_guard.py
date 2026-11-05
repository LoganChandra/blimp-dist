#!/usr/bin/env python3
"""
recall_guard.py — cheap faithfulness guards on the RECALL path (roadmap items
CRAG-lite, Self-RAG grounding gate, query decomposition). All opt-in; the pure
decision logic is dependency-free and unit-tested. Serves C8 (honesty) and
commitment #1 (never invent facts) by catching two failure modes:

  1. Answering from WEAK retrieval (nothing good came back, but we answer anyway).
  2. Stating a "recalled fact" a memory doesn't actually support.

Three tools:
  - recall_quality(rows): CRAG-lite. Grade retrieval from the rerank score we
    ALREADY compute (near-free, no LLM). Returns sufficient | widen | decompose.
  - is_compound(q) / decompose_query(q): split a multi-part question so each part
    gets its own focused recall instead of one under-specified search.
  - grounding_check(claim, memories): Self-RAG IsSUP — one cheap entailment call
    before a HIGH-STAKES assertion (pricing, client facts, "what did we decide").
    Gate it tightly; it is not meant to tax every turn.

None of this changes hindsight_client's default behaviour — it's a toolkit the
agent (or a guarded_recall wrapper) opts into for high-stakes recalls.

Self-test:  python recall_guard.py --selftest
"""
from __future__ import annotations

import os
import re
import sys
from typing import Optional

# CRAG-lite thresholds on the normalized cross-encoder rerank score (0..1).
SUFFICIENT_REL = float(os.environ.get("BRAIN_RECALL_SUFFICIENT", "0.55"))
WEAK_REL = float(os.environ.get("BRAIN_RECALL_WEAK", "0.30"))


def _env(key, default=None):
    try:
        for line in open(os.path.expanduser("~/blimp/.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(key, default)


# ---------------------------------------------------------------------------
# 1. CRAG-lite — grade retrieval quality from scores we already have
# ---------------------------------------------------------------------------

def recall_quality(rows: list[dict]) -> dict:
    """Judge whether recalled rows are good enough to answer from — reusing the
    rerank relevance (no extra model call).

      top >= SUFFICIENT_REL          -> "sufficient"
      WEAK_REL <= top < SUFFICIENT   -> "widen"   (recall more / lower the bar)
      top < WEAK_REL  or no rows      -> "decompose" (split or abstain)
    """
    scores = [r.get("relevance") for r in rows
              if isinstance(r, dict) and r.get("relevance") is not None]
    top = max(scores) if scores else 0.0
    if not rows or top < WEAK_REL:
        action = "decompose"
    elif top < SUFFICIENT_REL:
        action = "widen"
    else:
        action = "sufficient"
    return {"action": action, "top_relevance": round(top, 3),
            "n": len(rows), "sufficient": action == "sufficient"}


# ---------------------------------------------------------------------------
# 2. Query decomposition — split a compound question for focused recall
# ---------------------------------------------------------------------------

_COMPOUND_MARKERS = re.compile(
    r"\b(and|also|as well as|plus|compare|versus|vs\.?|both|either|"
    r"difference between)\b", re.I)
_INTERROG = re.compile(r"\b(what|when|where|who|whom|why|how|which)\b", re.I)


def is_compound(question: str) -> bool:
    """Heuristic: is this really several questions in one? Cheap gate so we only
    pay for decomposition when it's likely to help. Compound if: >1 '?', OR two
    interrogative pivots joined by a marker (what...and...what), OR a long
    question carrying a compare/and marker."""
    q = question or ""
    if q.count("?") > 1:
        return True
    has_marker = bool(_COMPOUND_MARKERS.search(q))
    if has_marker and len(_INTERROG.findall(q)) >= 2:
        return True
    if has_marker and len(q.split()) > 14:
        return True
    return False


def decompose_prompt(question: str) -> str:
    return (
        "Split this into the minimal set of standalone sub-questions needed to "
        "answer it (1 if already atomic, at most 4). One per line, no numbering.\n"
        f"Question: {question}\nSub-questions:"
    )


def decompose_query(question: str) -> list[str]:
    """Return sub-questions (>=1). Only calls the model when is_compound(); else
    returns [question]. Falls back to [question] on any error."""
    if not is_compound(question):
        return [question]
    key = _env("OPENROUTER_API_KEY")
    model = _env("BRAIN_DECOMPOSE_MODEL",
                 _env("HINDSIGHT_LLM_MODEL", "google/gemini-2.5-flash-lite"))
    if not key:
        return [question]
    try:
        import httpx
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0, "max_tokens": 200,
                  "messages": [{"role": "user", "content": decompose_prompt(question)}]},
            timeout=30,
        )
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"] or ""
        parts = [ln.strip(" -*0123456789.").strip()
                 for ln in txt.splitlines() if ln.strip()]
        parts = [p for p in parts if len(p) > 3][:4]
        return parts or [question]
    except Exception:
        return [question]


# ---------------------------------------------------------------------------
# 3. Grounding gate (Self-RAG IsSUP) — is a claim supported by memory?
# ---------------------------------------------------------------------------

def grounding_prompt(claim: str, memories: str) -> str:
    return (
        "Decide whether the MEMORY supports the CLAIM. 'supported' only if the "
        "memory states or directly entails the claim; 'unsupported' if it is "
        "silent, partial, or contradicts.\n\n"
        f"MEMORY:\n{memories[:3000] or '(nothing)'}\n\nCLAIM: {claim}\n\n"
        'Reply ONLY: {"supported": true|false, "reason": "<=12 words"}.'
    )


def grounding_check(claim: str, memories: str) -> dict:
    """One entailment call before a high-stakes assertion. Fails OPEN to
    'unknown' (never fabricates a 'supported'); the caller decides how strict."""
    import json
    key = _env("OPENROUTER_API_KEY")
    model = _env("BRAIN_GROUNDING_MODEL", "google/gemini-2.5-flash")
    if not key:
        return {"supported": None, "reason": "no key — grounding unavailable"}
    try:
        import httpx
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0, "max_tokens": 80,
                  "messages": [{"role": "user",
                                "content": grounding_prompt(claim, memories)}]},
            timeout=45,
        )
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"] or ""
        obj = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
        return {"supported": bool(obj.get("supported")),
                "reason": str(obj.get("reason", ""))[:100]}
    except Exception as e:
        return {"supported": None, "reason": f"grounding error: {str(e)[:50]}"}


# ---------------------------------------------------------------------------
# Self-test (pure logic; no network)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    check("strong retrieval -> sufficient",
          recall_quality([{"relevance": 0.8}, {"relevance": 0.4}])["action"] == "sufficient")
    check("medium retrieval -> widen",
          recall_quality([{"relevance": 0.42}])["action"] == "widen")
    check("weak retrieval -> decompose",
          recall_quality([{"relevance": 0.1}])["action"] == "decompose")
    check("no rows -> decompose",
          recall_quality([])["action"] == "decompose")

    check("atomic question not compound",
          not is_compound("What is the margin floor?"))
    check("multi-question detected compound",
          is_compound("What is the price and what is the timeline?"))
    check("long 'compare' question compound",
          is_compound("Can you compare the WhatsApp offer pricing against the "
                      "voice agent pricing for a new client"))
    check("atomic decomposes to itself (no call)",
          decompose_query("What is the margin floor?") == ["What is the margin floor?"])

    p = grounding_prompt("price is 1500", "logan set the price at 1500/mo")
    check("grounding prompt carries claim + memory",
          "price is 1500" in p and "1500/mo" in p)
    dp = decompose_prompt("a and b?")
    check("decompose prompt carries question", "a and b?" in dp)

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)


if __name__ == "__main__":
    main()
