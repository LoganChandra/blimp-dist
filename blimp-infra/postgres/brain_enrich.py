#!/usr/bin/env python3
"""
brain_enrich.py — two cheap, high-ROI upgrades to the Hindsight recall path,
both OFF by default (flag-gated) so they change nothing until we enable them.

1. CONTEXTUAL RETRIEVAL (write-time enrichment).  We embed memory text as-
   written, so a terse note like "use 1200 tokens" is unfindable by "MiniMax
   streaming config". Enriching the INDEXED text with situating context
   (who/when/why/supersedes/topics) before embedding cut failed retrievals
   49-67% in Anthropic's study — the highest ROI-per-hour on the roadmap.
     - enrich_deterministic(): free, uses metadata we already have. Safe always.
     - enrich_llm(): one cheap OpenRouter call for a one-line situating gloss.
       Enable with BRAIN_CONTEXTUAL_ENRICH=llm; costs a cheap call per write.

2. PER-SOURCE TRUST WEIGHTING (recall-time).  We store provenance (author +
   verified) but never USE it as a ranking signal. Multiplying a trust weight
   into the rerank relevance systematically down-ranks unverified memories —
   encoding the standing "verify Hermes's claims, his cheap model fabricates"
   rule as mechanism, not vigilance. Enable with BRAIN_TRUST_WEIGHTING=1.

Both are pure functions here; hindsight_client wires them in behind the flags.
No new infra, no data migration.

Self-test:  python brain_enrich.py --selftest
"""
from __future__ import annotations

import os
import sys
from typing import Optional

def _env(key, default=None):
    """Read a setting from ~/blimp/.env (the team config file — all three agents
    run as the same user, so they share it), then the process env. This is why
    setting a flag in ~/blimp/.env activates it for Duke, Frank AND Hermes on
    their next restart: one source of truth, no per-agent env drift."""
    try:
        for line in open(os.path.expanduser("~/blimp/.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(key, default)


# --- config (all default to inert; read from ~/blimp/.env, see _env) --------
CONTEXTUAL_ENRICH = (_env("BRAIN_CONTEXTUAL_ENRICH", "off") or "off").lower()  # off|deterministic|llm
TRUST_WEIGHTING = (_env("BRAIN_TRUST_WEIGHTING", "0") or "0") == "1"

VERIFIED_AUTHORS = {a.strip() for a in
                    (_env("BRAIN_VERIFIED_AUTHORS",
                          "duke,logan,frank,nanook") or "").split(",")}
# Trust multipliers applied to the rerank relevance. Verified peers rank at
# face value; unverified (e.g. Hermes's fabrication-prone cheap model) is damped.
TRUST_VERIFIED = float(os.environ.get("BRAIN_TRUST_VERIFIED", "1.0"))
TRUST_UNKNOWN = float(os.environ.get("BRAIN_TRUST_UNKNOWN", "0.85"))
TRUST_UNVERIFIED = float(os.environ.get("BRAIN_TRUST_UNVERIFIED", "0.7"))


# ---------------------------------------------------------------------------
# 1. Contextual enrichment (write-time)
# ---------------------------------------------------------------------------

def enrich_deterministic(content: str, author: Optional[str] = None,
                         tags=None, source: Optional[str] = None,
                         timestamp: Optional[str] = None,
                         supersedes: Optional[str] = None) -> str:
    """Prepend/append situating context we ALREADY have — zero cost, no LLM.
    Turns a terse memory into a self-describing one for the embedder + BM25."""
    lead = []
    if timestamp:
        lead.append(f"On {timestamp},")
    if author:
        lead.append(f"{author} noted")
    prefix = " ".join(lead)
    tail = []
    if source:
        tail.append(f"source: {source}")
    if tags:
        tag_str = tags if isinstance(tags, str) else ", ".join(tags)
        if tag_str:
            tail.append(f"topics: {tag_str}")
    if supersedes:
        tail.append(f"supersedes: {supersedes[:100]}")
    body = f"{prefix} {content}".strip() if prefix else content
    return f"{body}  [{'; '.join(tail)}]" if tail else body


def build_llm_prompt(content: str, context: Optional[str] = None) -> str:
    """The situating-gloss prompt (separated so it's testable without a call)."""
    ctx = f"\nSurrounding context:\n{context[:1500]}\n" if context else ""
    return (
        "Write ONE short sentence that situates the memory below so it can be "
        "found later by search: who/what it is about, when, and why it matters. "
        "Do not repeat the memory verbatim; add the missing context only.\n"
        f"{ctx}\nMemory: {content}\n\nSituating sentence:"
    )


def enrich_llm(content: str, context: Optional[str] = None) -> str:
    """One cheap OpenRouter call to prepend a situating sentence. Falls back to
    the deterministic enrichment (never raises) so a write is never blocked."""
    key = _env("OPENROUTER_API_KEY")
    model = _env("BRAIN_ENRICH_MODEL", _env("HINDSIGHT_LLM_MODEL",
                                            "google/gemini-2.5-flash-lite"))
    if not key:
        return enrich_deterministic(content)
    try:
        import httpx
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "temperature": 0, "max_tokens": 80,
                  "messages": [{"role": "user",
                                "content": build_llm_prompt(content, context)}]},
            timeout=30,
        )
        r.raise_for_status()
        gloss = (r.json()["choices"][0]["message"]["content"] or "").strip()
        return f"{gloss}\n{content}" if gloss else content
    except Exception:
        return enrich_deterministic(content)


def enrich(content: str, mode: Optional[str] = None, **meta) -> str:
    """Dispatch on the configured mode. mode=None reads the env flag; 'off'
    returns content unchanged (the default — inert until we opt in)."""
    mode = (mode or CONTEXTUAL_ENRICH).lower()
    if mode == "llm":
        return enrich_llm(content, context=meta.get("context"))
    if mode == "deterministic":
        return enrich_deterministic(
            content, author=meta.get("author"), tags=meta.get("tags"),
            source=meta.get("source"), timestamp=meta.get("timestamp"),
            supersedes=meta.get("supersedes"))
    return content


# ---------------------------------------------------------------------------
# 2. Trust weighting (recall-time)
# ---------------------------------------------------------------------------

def _as_bool(v):
    """Coerce the verified signal to a real bool. Hindsight stringifies all
    metadata, so `verified` arrives as "True"/"False" (or None) on recall — the
    identity checks below would otherwise never match and the explicit signal
    would be silently ignored. Returns True/False/None."""
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("true", "t", "1", "yes"):
        return True
    if s in ("false", "f", "0", "no"):
        return False
    return None


def trust_weight(author: Optional[str], verified) -> float:
    verified = _as_bool(verified)
    if verified is True:
        return TRUST_VERIFIED
    if verified is False:
        return TRUST_UNVERIFIED
    # unknown verified status: trust known verified-list authors, damp the rest
    if author and str(author).lower() in VERIFIED_AUTHORS:
        return TRUST_VERIFIED
    return TRUST_UNKNOWN if not author or author == "unknown" else TRUST_UNVERIFIED


def apply_trust_weights(rows: list[dict], force: bool = False) -> list[dict]:
    """Multiply each row's relevance by its source trust weight and re-sort.
    Inert unless BRAIN_TRUST_WEIGHTING=1 (or force=True). Adds
    'trust_weight' + 'relevance_weighted' for transparency; never drops rows."""
    if not (TRUST_WEIGHTING or force):
        return rows
    for r in rows:
        if not isinstance(r, dict):
            continue
        md = r.get("metadata") or {}
        author = r.get("author") or md.get("author")
        verified = r.get("verified")
        if verified is None:
            verified = md.get("verified")
        w = trust_weight(author, verified)
        rel = r.get("relevance")
        r["trust_weight"] = round(w, 3)
        r["relevance_weighted"] = round((rel or 0.0) * w, 4) if rel is not None else None
    rows.sort(key=lambda r: (r.get("relevance_weighted")
                             if isinstance(r, dict) and r.get("relevance_weighted") is not None
                             else -1.0), reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Self-test (pure logic, no network)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    global TRUST_WEIGHTING
    fails = []

    def check(name, cond):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    e = enrich_deterministic("use 1200 tokens", author="duke",
                             tags="minimax,streaming", timestamp="2026-07-11",
                             source="blimp_brain_mcp.py")
    check("deterministic enrich adds who/when/topics",
          "duke noted" in e and "2026-07-11" in e and "minimax" in e
          and "use 1200 tokens" in e)

    check("enrich mode=off is inert",
          enrich("raw text", mode="off") == "raw text")

    p = build_llm_prompt("use 1200 tokens", context="MiniMax M2 config")
    check("llm prompt includes memory + context",
          "use 1200 tokens" in p and "MiniMax" in p)

    # trust weighting: an unverified hit that out-scores a verified one on raw
    # relevance should drop BELOW it once trust is applied.
    rows = [
        {"text": "hermes says price is 900", "relevance": 0.80,
         "author": "hermes", "verified": False},
        {"text": "logan set price at 1500", "relevance": 0.74,
         "author": "logan", "verified": True},
    ]
    out = apply_trust_weights(rows, force=True)
    check("verified fact outranks higher-relevance unverified one after weighting",
          out[0]["author"] == "logan")
    check("weights recorded for transparency",
          out[0]["trust_weight"] == 1.0 and out[1]["trust_weight"] == TRUST_UNVERIFIED)

    # Hindsight stringifies metadata: verified arrives as "True"/"False".
    # Coercion must still down-rank the unverified one.
    rows_str = [
        {"text": "hermes claim", "relevance": 0.80, "author": "hermes", "verified": "False"},
        {"text": "logan fact", "relevance": 0.74, "author": "logan", "verified": "True"},
    ]
    out_str = apply_trust_weights(rows_str, force=True)
    check("stringified verified is coerced (verified fact wins)",
          out_str[0]["author"] == "logan")
    check("string 'False' damped like a real False",
          out_str[1]["trust_weight"] == TRUST_UNVERIFIED)

    # inert when the flag is off — force it off locally so this holds regardless
    # of the deployed ~/blimp/.env setting.
    _saved, TRUST_WEIGHTING = TRUST_WEIGHTING, False
    rows2 = [{"text": "a", "relevance": 0.5, "author": "hermes", "verified": False}]
    before = list(rows2)
    check("trust weighting inert when flag off", apply_trust_weights(rows2) == before)
    TRUST_WEIGHTING = _saved

    print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
    return 1 if fails else 0


def main():
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)


if __name__ == "__main__":
    main()
