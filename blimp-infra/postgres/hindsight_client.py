"""
hindsight_client.py — Duke's long-term memory CLI (thin wrapper over Hindsight HTTP API).

Hindsight runs as a Docker container exposing an HTTP API on http://localhost:8888.
This script is the interface any agent (Claude Code, Hermes, Aider, custom) uses
to read/write Duke's semantic memory.

Architecture note: Hindsight organises everything into "memory banks". Duke gets
a single bank named "duke" (override via $DUKE_BANK). All memories, conversation
logs, and ingested docs live inside that bank, scoped by tags.

Usage:
    duke-mem init-bank                            # first-time setup (idempotent)
    duke-mem recall "<query>" [--limit 5]
    duke-mem remember --content "<text>" [--source "<path>"] [--tags tag1,tag2]
    duke-mem log --user logan --channel other-agent --speaker duke --message "..."
    duke-mem recent [--channel other-agent] [--limit 20]
    duke-mem ingest <path>                        # ingest one file
    duke-mem ingest-repo [--root <dir>]           # walk repo and ingest all .md files
    duke-mem status                               # check Hindsight is reachable
    duke-mem stats                                # bank stats (memory count, etc.)
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

import httpx


HINDSIGHT_URL = os.getenv("HINDSIGHT_URL", "http://localhost:8888")
BANK_ID = os.getenv("DUKE_BANK", "duke")
DEFAULT_USER = os.getenv("HINDSIGHT_USER", "duke")
TIMEOUT = 60.0

# --- Provenance + dedup-on-write (Frank, 2026-06-23 brain P0) --------------
# Grounded in the real Hindsight recall pipeline: with trace=true the response
# exposes trace.reranked[*].rerank_score (normalized cross-encoder, 0..1),
# joinable to results[*].id by node_id. We use that score to GATE dedup, then
# confirm with client-side text similarity so distinct-but-related facts are
# never merged (only genuine near/verbatim duplicates are).
import datetime          # noqa: E402
import difflib           # noqa: E402
try:
    # Contextual write-time enrichment + recall-time trust weighting.
    # Both flag-gated and default-OFF (BRAIN_CONTEXTUAL_ENRICH / BRAIN_TRUST_WEIGHTING),
    # so importing this changes nothing until we opt in.
    import brain_enrich          # noqa: E402
except Exception:
    brain_enrich = None

# Calibrated for Cohere rerank-v3.5 (2026-06-23): a near-exact self-match scores
# ~0.90 and distinct/weak facts ~0.03, so the RERANK SCORE is the primary dedup
# signal. (The old local ms-marco scale was ~1.1 / ~0.003 — if you ever swap the
# reranker, override these via the BRAIN_DEDUP_* env vars; no code change needed.)
# Text similarity stays a loose confirm because Hindsight rewrites content into facts.
DEDUP_REL_GATE   = float(os.getenv("BRAIN_DEDUP_REL_GATE", "0.82"))   # nearest memory's rerank relevance must hit this
DEDUP_SKIP_REL   = float(os.getenv("BRAIN_DEDUP_SKIP_REL", "0.87"))   # near-exact -> skip the insert (reinforce)
DEDUP_SKIP_TEXT  = float(os.getenv("BRAIN_DEDUP_SKIP_TEXT", "0.70"))
DEDUP_AMEND_TEXT = float(os.getenv("BRAIN_DEDUP_AMEND_TEXT", "0.50")) # near-dup update -> "As of <date>:" amend
MMR_DUP_RATIO    = float(os.getenv("BRAIN_MMR_DUP_RATIO", "0.85"))    # recall: drop near-duplicate results (diversity)
VERIFIED_AUTHORS  = {a.strip() for a in
                     os.getenv("BRAIN_VERIFIED_AUTHORS", "duke,logan,frank,nanook").split(",")}


def _kl_today() -> str:
    """Asia/Kuala_Lumpur date for 'As of <date>:' amendments."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.date.today().isoformat()


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _text_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _scope_visible(item: dict, viewer: str) -> bool:
    """Per-agent scope visibility. metadata.scope is one of: absent/'team'
    (everyone sees it), 'role:<x>' (team-visible in v1), 'private:<agent>'
    (only that agent). viewer is the requesting agent name."""
    if not isinstance(item, dict):
        return True
    sc = (item.get("metadata") or {}).get("scope")
    if not sc:
        # tags survive Hindsight's extraction even when custom metadata doesn't
        for t in (item.get("tags") or []):
            if isinstance(t, str) and t.startswith("scope:"):
                sc = t[len("scope:"):]; break
    if not sc or sc == "team" or str(sc).startswith("role:"):
        return True
    if str(sc).startswith("private:"):
        return str(sc).split(":", 1)[1] == viewer
    return True


# Hindsight API is bank-scoped. Build paths via _bank_path() so changing
# BANK_ID via env propagates everywhere.
def _bank_path(suffix: str = "") -> str:
    return f"/v1/default/banks/{BANK_ID}{suffix}"


HEALTH_PATH = "/health"


# ---------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------

def _post(path: str, body: dict) -> dict:
    r = httpx.post(HINDSIGHT_URL + path, json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        # surface server detail in the error so debugging is sane
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise httpx.HTTPStatusError(
            f"{r.status_code} on POST {path}: {detail}",
            request=r.request, response=r,
        )
    return r.json() if r.content else {}


def _put(path: str, body: dict) -> dict:
    r = httpx.put(HINDSIGHT_URL + path, json=body, timeout=TIMEOUT)
    if r.status_code >= 400:
        try:
            detail = r.json()
        except Exception:
            detail = r.text
        raise httpx.HTTPStatusError(
            f"{r.status_code} on PUT {path}: {detail}",
            request=r.request, response=r,
        )
    return r.json() if r.content else {}


def _get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(HINDSIGHT_URL + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------
# Bank lifecycle
# ---------------------------------------------------------------------

DUKE_RETAIN_MISSION = (
    "You are Duke's long-term memory. Extract durable facts, decisions, "
    "preferences, and project context. Prioritise: client commitments, "
    "pricing decisions, technical architecture choices, the Boss's preferences "
    "and feedback, and anything the Boss flags as important. Ignore meeting "
    "logistics and conversational filler."
)

DUKE_OBSERVATIONS_MISSION = (
    "Observations are stable facts about the Boss, clients, prospects, and "
    "Blimp's architecture. Include preferences, working style, recurring "
    "concerns, and decisions that affect future work."
)


def init_bank() -> dict:
    """Idempotent — PUT replaces config each time."""
    return _put(_bank_path(), {
        "retain_mission": DUKE_RETAIN_MISSION,
        "observations_mission": DUKE_OBSERVATIONS_MISSION,
        "retain_extraction_mode": "concise",
        "enable_observations": True,
    })


def get_bank() -> dict:
    # Hindsight 0.10+ removed GET /banks/{id} (405); the bank's settings live at /config.
    return _get(_bank_path("/config"))


def bank_stats() -> dict:
    return _get(_bank_path("/stats"))


# ---------------------------------------------------------------------
# Memory operations
# ---------------------------------------------------------------------

def _memory_item(content: str,
                 context: Optional[str] = None,
                 tags: Optional[list[str]] = None,
                 metadata: Optional[dict] = None,
                 document_id: Optional[str] = None,
                 timestamp: Optional[str] = None,
                 update_mode: str = "replace") -> dict:
    """Build a MemoryItem dict. Hindsight's MemoryItem.metadata requires string-typed
    values, so coerce everything to strings."""
    item: dict = {"content": content}
    if context:    item["context"] = context
    if tags:       item["tags"] = tags
    if metadata:   item["metadata"] = {k: str(v) for k, v in metadata.items()}
    if document_id: item["document_id"] = document_id
    if timestamp:  item["timestamp"] = timestamp
    if update_mode: item["update_mode"] = update_mode
    return item


def remember(content: str,
             source: Optional[str] = None,
             tags: Optional[list[str]] = None,
             context: Optional[str] = None,
             metadata: Optional[dict] = None,
             document_id: Optional[str] = None,
             author: Optional[str] = None,
             verified: Optional[bool] = None,
             scope: Optional[str] = None,
             importance: Optional[str] = None,
             dedup: bool = True) -> dict:
    """Write a memory with provenance + dedup-on-write.

    - Stamps metadata.author / metadata.verified and an author:<x> tag.
    - dedup-on-write (skipped when a document_id is supplied, e.g. docs/chat):
      recall the nearest existing memory; if the reranker rates it >= the
      relevance gate AND the text is a near/verbatim duplicate, reinforce
      (skip) or amend ("As of <date>: ...") instead of blindly appending.
      Any failure falls back to a plain append — a write is never blocked.
    """
    meta = dict(metadata or {})
    if source:
        meta["source"] = source

    # provenance
    author = author or os.getenv("A2A_AGENT_NAME") or DEFAULT_USER
    meta.setdefault("author", author)
    if verified is None:
        verified = str(author).lower() in VERIFIED_AUTHORS
    meta.setdefault("verified", verified)
    tags = list(tags or [])
    if f"author:{author}" not in tags:
        tags.append(f"author:{author}")
    # scope (visibility): team | role:<x> | private:<agent>. Default unset = team.
    if scope:
        meta.setdefault("scope", scope)
        if f"scope:{scope}" not in tags:
            tags.append(f"scope:{scope}")
    # importance / salience (bounded + explainable): low | normal | high
    if importance:
        meta.setdefault("importance", importance)
        if f"importance:{importance}" not in tags:
            tags.append(f"importance:{importance}")

    # dedup + contradiction handling (best-effort; never blocks the write).
    # Primary signal is the reranker relevance of the nearest existing memory;
    # text similarity is a loose confirm (Hindsight rewrites content into facts).
    if dedup and not document_id:
        try:
            top = recall(content, limit=1, budget="low")
            if top and isinstance(top[0], dict):
                cand = top[0]
                rel = cand.get("relevance")
                cand_text = cand.get("text") or cand.get("content") or ""
                cand_kind = (cand.get("metadata") or {}).get("kind")
                cand_author = _author_of(cand)
                ratio = _text_ratio(content, cand_text)
                if cand_kind != "chat" and isinstance(rel, (int, float)) and rel >= DEDUP_REL_GATE:
                    if cand_author in (author, "unknown"):
                        # same author -> self-update: skip verbatim, else amend
                        if rel >= DEDUP_SKIP_REL and ratio >= DEDUP_SKIP_TEXT:
                            return {"deduped": "skipped_duplicate",
                                    "matched_id": cand.get("id"),
                                    "relevance": round(rel, 3), "text_ratio": round(ratio, 3)}
                        if ratio >= DEDUP_AMEND_TEXT:
                            if cand.get("id"):
                                meta["supersedes_id"] = str(cand.get("id"))
                            meta["supersedes"] = (cand_text or "")[:500]
                            content = f"As of {_kl_today()}: {content}"
                    elif ratio >= DEDUP_AMEND_TEXT:
                        # DIFFERENT author on a closely-related claim. NEVER
                        # overwrite a peer (damping: no cross-author supersede
                        # oscillation, and text-similarity can't tell "70%" from
                        # "80%"). Keep BOTH, link them, and tag 'contested' so
                        # disagreements() can surface it for adjudication.
                        if cand.get("id"):
                            meta["peer_claim_id"] = str(cand.get("id"))
                        meta["peer_author"] = cand_author
                        if "contested" not in tags:
                            tags.append("contested")
        except Exception:
            pass  # never block a write on dedup failure

    # Contextual retrieval: enrich the INDEXED text with situating context so a
    # terse memory stays findable. Inert unless BRAIN_CONTEXTUAL_ENRICH is set.
    stored = content
    if brain_enrich is not None:
        try:
            stored = brain_enrich.enrich(
                content, author=author, tags=tags,
                source=meta.get("source"), supersedes=meta.get("supersedes"))
        except Exception:
            stored = content  # never block a write on enrichment
    item = _memory_item(stored, context=context, tags=tags,
                        metadata=meta, document_id=document_id)
    return _post(_bank_path("/memories"), {"items": [item], "async": False})


def recall(query: str, limit: int = 5,
           tags: Optional[list[str]] = None,
           budget: str = "mid",
           trace: bool = True,
           scope: Optional[str] = None,
           min_relevance: Optional[float] = None,
           diversify: bool = True) -> list[dict]:
    """Ranked semantic recall with relevance scores, scope filtering, MMR
    diversity, and an abstention floor.

    - relevance (0..1 normalized cross-encoder) + rank joined onto each result.
    - scope: keep only memories visible to this viewer (e.g. "frank") — see
      _scope_visible. None = no scope filter.
    - min_relevance: drop hits below this score (abstention — never surface a
      weak match as if confident).
    - diversify: drop near-duplicate results (MMR-style) so the window isn't
      eaten by paraphrases of one fact.
    """
    body: dict = {
        "query": query,
        "budget": budget,
        # Honor `limit`: request roughly enough tokens for the asked-for count
        # (~700 tok/result), clamped — replaces the old lossy 2-bucket map.
        "max_tokens": max(1024, min(8192, int(limit) * 700)),
    }
    if trace:
        body["trace"] = True
    if tags:
        body["tags"] = tags
        body["tags_match"] = "any"
    result = _post(_bank_path("/memories/recall"), body)

    # Scores live in trace.reranked[*] keyed by node_id (== results[*].id):
    # rerank_score is the normalized cross-encoder relevance (0..1).
    score_by_id: dict = {}
    if isinstance(result, dict) and isinstance(result.get("trace"), dict):
        for r in (result["trace"].get("reranked") or []):
            nid = r.get("node_id") or r.get("id")
            if nid is not None:
                score_by_id[nid] = (r.get("rerank_score"), r.get("rerank_rank"))

    # RecallResponse shape varies by Hindsight version; find the ranked list.
    items = None
    if isinstance(result, dict):
        for key in ("results", "memories", "facts", "items"):
            if isinstance(result.get(key), list):
                items = result[key]; break
        if items is None and isinstance(result.get("data"), dict):
            for key in ("results", "memories", "facts", "items"):
                if isinstance(result["data"].get(key), list):
                    items = result["data"][key]; break
    if items is None:
        items = result if isinstance(result, list) else ([result] if result else [])

    # Attach relevance + rank so the caller can see how good each hit is.
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        rel, rnk = score_by_id.get(it.get("id"), (None, None))
        if it.get("relevance") is None and rel is not None:
            it["relevance"] = rel
        if it.get("rank") is None:
            it["rank"] = rnk if rnk is not None else i + 1

    # Per-source trust weighting: down-rank unverified memories (encodes the
    # standing "verify Hermes's claims" rule as ranking). Inert unless
    # BRAIN_TRUST_WEIGHTING=1. Pre-attach provenance so the weighter can read it.
    if brain_enrich is not None and getattr(brain_enrich, "TRUST_WEIGHTING", False):
        for it in items:
            if isinstance(it, dict):
                p = provenance_of(it)
                it.setdefault("author", p["author"])
                if it.get("verified") is None:
                    it["verified"] = p["verified"]
        items = brain_enrich.apply_trust_weights(items)

    # scope visibility filter
    if scope:
        items = [it for it in items if _scope_visible(it, scope)]

    # abstention: drop weak hits below the floor
    if min_relevance is not None:
        items = [it for it in items if not isinstance(it, dict)
                 or it.get("relevance") is None or it.get("relevance") >= min_relevance]

    # MMR diversity: drop near-duplicate results, keep the higher-ranked one
    if diversify:
        kept: list = []
        for it in items:
            if isinstance(it, dict):
                t = it.get("text") or it.get("content") or ""
                if any(isinstance(k, dict)
                       and _text_ratio(t, k.get("text") or k.get("content") or "") >= MMR_DUP_RATIO
                       for k in kept):
                    continue
            kept.append(it)
        items = kept

    return items[:limit]


def list_memories(tags: Optional[list[str]] = None, limit: int = 50) -> list[dict]:
    params: dict = {"limit": limit}
    if tags:
        # Hindsight wants repeated ?tags=a&tags=b plus tags_match=all. A comma-joined string was read as one
        # tag ("conversation,channel:duke") that nothing has, so recent(channel=...) always came back empty
        # and agents could not see each other's conversations (found 2026-09-27).
        params["tags"] = list(tags)
        params["tags_match"] = "all"
    result = _get(_bank_path("/memories/list"), params)
    if isinstance(result, dict):
        for key in ("results", "memories", "items", "data"):
            v = result.get(key)
            if isinstance(v, list):
                return v
    return result if isinstance(result, list) else [result]


# ---------------------------------------------------------------------
# Theory of mind — who believes what, and where agents disagree.
# Built on author-provenance tags (author:<x>), which survive Hindsight's
# extraction even when custom metadata does not. Scales to N agents.
# ---------------------------------------------------------------------

def _author_of(item: dict) -> str:
    md = (item.get("metadata") or {}) if isinstance(item, dict) else {}
    a = md.get("author")
    if not a and isinstance(item, dict):
        for t in (item.get("tags") or []):
            if isinstance(t, str) and t.startswith("author:"):
                return t[len("author:"):]
    return a or "unknown"


def _verified_of(item: dict):
    return ((item.get("metadata") or {}) if isinstance(item, dict) else {}).get("verified")


def provenance_of(item: dict) -> dict:
    """Unified provenance read — metadata first, tags fallback — so author /
    verified / scope / importance surface CONSISTENTLY everywhere regardless of
    whether Hindsight propagated the custom metadata onto this unit."""
    md = (item.get("metadata") or {}) if isinstance(item, dict) else {}
    tags = (item.get("tags") or []) if isinstance(item, dict) else []

    def from_tag(prefix):
        for t in tags:
            if isinstance(t, str) and t.startswith(prefix):
                return t[len(prefix):]
        return None

    return {
        "author": md.get("author") or from_tag("author:") or "unknown",
        "verified": md.get("verified"),
        "scope": md.get("scope") or from_tag("scope:"),
        "importance": md.get("importance") or from_tag("importance:"),
    }


def agent_beliefs(agent: str, query: Optional[str] = None, limit: int = 20) -> list[dict]:
    """What a specific agent has asserted (their 'view') — filtered by author tag."""
    if query:
        return recall(query, limit=limit, tags=[f"author:{agent}"], diversify=False)
    return list_memories(tags=[f"author:{agent}"], limit=limit)


def who_believes(query: str, limit: int = 12) -> dict:
    """Group what's known on a topic BY AUTHOR — 'who believes what'."""
    out: dict = {}
    for r in recall(query, limit=limit, diversify=False):
        if isinstance(r, dict):
            out.setdefault(_author_of(r), []).append({
                "text": r.get("text") or r.get("content"),
                "relevance": r.get("relevance"),
                "verified": _verified_of(r),
            })
    return out


def disagreements(query: str, limit: int = 16, min_relevance: float = 0.3) -> dict:
    """Surface claims on a topic held by DIFFERENT authors — candidate
    disagreements for a human/agent to adjudicate. 'contested' = >1 author."""
    claims = [{"author": _author_of(r), "text": r.get("text") or r.get("content"),
               "relevance": r.get("relevance"), "verified": _verified_of(r)}
              for r in recall(query, limit=limit, diversify=True, min_relevance=min_relevance)
              if isinstance(r, dict)]
    authors = sorted({c["author"] for c in claims if c["author"] != "unknown"})
    return {"query": query, "distinct_authors": authors,
            "contested": len(authors) > 1, "claims": claims}


# ---------------------------------------------------------------------
# Conversation log (tagged memories — Hindsight has no separate convo table)
# ---------------------------------------------------------------------

def log_message(user_id: str, channel: str, speaker: str, message: str,
                metadata: Optional[dict] = None) -> dict:
    meta = dict(metadata or {})
    meta.update({"user": user_id, "channel": channel, "speaker": speaker, "kind": "chat"})
    content = f"[{speaker}] {message}"
    tags = ["conversation", f"channel:{channel}", f"speaker:{speaker}"]
    # Unique document_id per message. The old fixed "chat:{channel}" + replace
    # mode clobbered each channel down to a single row; each turn is now its own
    # append-only episodic memory. Provenance author = the speaker.
    import time
    doc_id = f"chat:{channel}:{time.time_ns()}"
    return remember(content, tags=tags, metadata=meta,
                    document_id=doc_id, context=f"chat in {channel}",
                    author=speaker, verified=None, dedup=False)


def _when(item: dict) -> str:
    return str(item.get("occurred_start") or item.get("date") or item.get("mentioned_at") or "")


def recent(channel: Optional[str] = None, user_id: Optional[str] = None,
           limit: int = 20) -> list[dict]:
    """Newest conversation turns first. Hindsight's list endpoint is not ordered by time, so pull a wide
    window and sort here; otherwise "what did the Boss last say to Frank" returns a random old turn."""
    tags = ["conversation"]
    if channel:
        tags.append(f"channel:{channel}")
    rows = list_memories(tags=tags, limit=max(500, limit))
    rows = [r for r in rows if isinstance(r, dict)]
    rows.sort(key=_when, reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------

def health() -> bool:
    try:
        r = httpx.get(HINDSIGHT_URL + HEALTH_PATH, timeout=5.0)
        return r.status_code == 200
    except Exception as e:
        print(f"Hindsight not reachable at {HINDSIGHT_URL}: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------
# Repo ingestion (chunk by H2, hash for dedup, batch-post per file)
# ---------------------------------------------------------------------

INGEST_DIRS = ["strategy", "sales", "build", "reference", ".agent-mail",
               "docs", "clients", ".claude/skills"]
INGEST_FILES = ["CLAUDE.md", "AGENTS.md", "README.md",
                "blimp-infra/duke-system-context.md", "blimp-infra/README.md",
                "blimp-infra/hermes-system-context.md"]
SKIP_PATTERNS = [r"/archive/", r"/node_modules/", r"/\.git/",
                 r"/pdfs/", r"/blimp-infra/postgres/", r"/blimp-infra/secrets/",
                 r"/\.mempalace/"]


def should_skip(path: str) -> bool:
    norm = path.replace(os.sep, "/")
    return any(re.search(p, norm) for p in SKIP_PATTERNS)


def chunk_doc(text: str, max_chars: int = 1800) -> list[tuple[str, str]]:
    sections, buf, current_heading = [], [], "Intro"
    for line in text.splitlines():
        if line.startswith("## "):
            if buf:
                sections.append((current_heading, "\n".join(buf).strip()))
                buf = []
            current_heading = line[3:].strip()
        else:
            buf.append(line)
    if buf:
        sections.append((current_heading, "\n".join(buf).strip()))

    out = []
    for heading, body in sections:
        if not body: continue
        if len(body) <= max_chars:
            out.append((heading, body))
        else:
            paras, sub, sub_len = body.split("\n\n"), [], 0
            for p in paras:
                if sub_len + len(p) > max_chars and sub:
                    out.append((heading, "\n\n".join(sub))); sub = [p]; sub_len = len(p)
                else:
                    sub.append(p); sub_len += len(p)
            if sub: out.append((heading, "\n\n".join(sub)))
    return out


def ingest_file(path: Path, repo_root: Path) -> int:
    """One MemoryItem per file. Hindsight does its own chunking internally
    (controlled by retain_chunk_size in the bank config), so we don't need
    to pre-chunk. document_id is stable per-file → re-ingest cleanly replaces."""
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return 0
    rel = str(path.relative_to(repo_root)).replace(os.sep, "/")
    item = _memory_item(
        f"# {rel}\n\n{text}",
        tags=["doc", path.parent.name, "repo"],
        metadata={"source": rel, "kind": "doc",
                  "source_hash": hashlib.sha256(text.encode()).hexdigest()},
        document_id=f"doc:{rel}",
        update_mode="replace",
    )
    _post(_bank_path("/memories"), {"items": [item], "async": True})
    return 1


def ingest_repo(root: Path) -> None:
    targets = []
    for d in INGEST_DIRS:
        targets.extend(root.glob(f"{d}/**/*.md"))
    for f in INGEST_FILES:
        p = root / f
        if p.exists(): targets.append(p)
    targets = [p for p in targets if not should_skip(str(p))]

    print(f"Ingesting {len(targets)} files into Hindsight bank '{BANK_ID}' ({HINDSIGHT_URL})...")
    total = 0
    for path in sorted(targets):
        try:
            n = ingest_file(path, root)
        except httpx.HTTPStatusError as e:
            print(f"  ! {path.relative_to(root)}: {e}", file=sys.stderr)
            continue
        if n:
            print(f"  + {path.relative_to(root)}: queued")
            total += n
    print(f"Done. Queued {total} files for async processing. Hindsight will chunk + embed in background.")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="duke-mem",
        description="Duke's long-term memory — semantic recall, conversation log, doc ingestion (via Hindsight)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-bank")
    sub.add_parser("status")
    sub.add_parser("stats")

    p = sub.add_parser("recall");   p.add_argument("query"); p.add_argument("--limit", type=int, default=5); p.add_argument("--tags"); p.add_argument("--budget", default="mid")
    p = sub.add_parser("remember"); p.add_argument("--content", required=True); p.add_argument("--source"); p.add_argument("--tags"); p.add_argument("--context"); p.add_argument("--doc-id", dest="doc_id")
    p = sub.add_parser("log");      p.add_argument("--user", required=True); p.add_argument("--channel", required=True); p.add_argument("--speaker", required=True); p.add_argument("--message", required=True)
    p = sub.add_parser("recent");   p.add_argument("--channel"); p.add_argument("--user"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("ingest");   p.add_argument("path")
    p = sub.add_parser("ingest-repo"); p.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))

    args = parser.parse_args()

    if args.cmd == "status":
        ok = health()
        print(f"Hindsight @ {HINDSIGHT_URL}: {'ok' if ok else 'unreachable'}")
        sys.exit(0 if ok else 1)

    if args.cmd == "init-bank":
        result = init_bank()
        print(f"Bank '{BANK_ID}' initialised.")
        print(json.dumps(result, indent=2)[:500])
        return

    if args.cmd == "stats":
        try:
            s = bank_stats()
            print(json.dumps(s, indent=2))
        except httpx.HTTPStatusError as e:
            print(f"Stats unavailable (bank may not exist yet — run `duke-mem init-bank`): {e}", file=sys.stderr)
            sys.exit(1)
        return

    if args.cmd == "recall":
        tags = args.tags.split(",") if args.tags else None
        results = recall(args.query, args.limit, tags=tags, budget=args.budget)
        if not results:
            print("(no results)")
            return
        for r in results:
            score = r.get("score") or r.get("similarity") or r.get("relevance") or ""
            content = r.get("content") or r.get("text") or r.get("fact") or ""
            preview = content[:240]
            src = ""
            md = r.get("metadata") or {}
            if isinstance(md, dict):
                src = md.get("source") or md.get("document_id") or ""
            print(f"[{score}] {src}")
            print(f"  {preview}{'...' if len(preview) >= 240 else ''}\n")

    elif args.cmd == "remember":
        tags = args.tags.split(",") if args.tags else None
        result = remember(args.content, source=args.source, tags=tags,
                          context=args.context, document_id=args.doc_id)
        print(json.dumps(result, indent=2)[:500])

    elif args.cmd == "log":
        result = log_message(args.user, args.channel, args.speaker, args.message)
        print(json.dumps(result, indent=2)[:500])

    elif args.cmd == "recent":
        rows = recent(args.channel, args.user, args.limit)
        if not rows:
            print("(no conversation log entries)")
            return
        for r in rows:
            if not isinstance(r, dict):
                print(r); continue
            # list_memories returns wrapper objects; the actual fact/content may live
            # at the top level or under 'fact', 'memory', 'data', or 'unit'.
            inner = r
            for k in ("memory", "fact", "unit", "data"):
                if isinstance(r.get(k), dict):
                    inner = {**inner, **r[k]}
            ts = (inner.get("created_at") or inner.get("timestamp")
                  or inner.get("updated_at") or "")
            md = inner.get("metadata") or {}
            if not isinstance(md, dict): md = {}
            chan = md.get("channel", "?")
            speaker = md.get("speaker", "?")
            content = (inner.get("content") or inner.get("text")
                       or inner.get("fact_content") or "")
            if not content:
                # Last resort: dump the keys so we can see what shape we got
                content = f"(raw keys: {list(inner.keys())})"
            print(f"[{ts}] {chan}/{speaker}: {content[:160]}")

    elif args.cmd == "ingest":
        p = Path(args.path).resolve()
        if not p.exists():
            print(f"File not found: {p}", file=sys.stderr); sys.exit(1)
        n = ingest_file(p, p.parent)
        print(f"Ingested {n} chunks from {p.name}")

    elif args.cmd == "ingest-repo":
        ingest_repo(Path(args.root))


if __name__ == "__main__":
    main()
