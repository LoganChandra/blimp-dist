#!/usr/bin/env python3
"""
datasets.py — load the real public memory benchmarks and normalise them to one
schema the runner understands.

A benchmark is a list of "groups". A group shares one haystack (conversation
history) and carries one or more questions. The runner builds ONE isolated bank
per group, ingests its sessions, then answers every question against it.

  Group = {
    "group_id": str,
    "suite":    "locomo" | "longmemeval",
    "turns":    [ {"speaker","text","timestamp","session"} , ... ],   # ingest order
    "questions":[ {"id","category","question","answer","question_date"} , ... ],
  }

LOCOMO   (snap-research/locomo, locomo10.json): 10 conversations, ~200 QA pairs.
         One group per conversation (efficient: many Qs share one haystack).
LongMemEval (xiaowu0162/LongMemEval, longmemeval_{s,oracle}.json): 500 Qs, each
         with its own haystack. One group per question.

Run harness/fetch_datasets.sh first to download the raw files into harness/data/.
"""
from __future__ import annotations

import json
import os
import re
import datetime
from typing import Optional


def _to_iso(s) -> Optional[str]:
    """Normalise a dataset timestamp to ISO 8601 (Hindsight rejects anything
    else). Handles LOCOMO ('1:56 pm on 8 May, 2023') and LongMemEval
    ('2023/05/20 (Sat) 02:21'); returns None if unparseable (ingest then just
    omits the timestamp rather than 422-ing the whole batch)."""
    if not s:
        return None
    raw = str(s).strip()
    cleaned = re.sub(r"\s*\([^)]*\)\s*", " ", raw).strip()  # drop "(Sat)" etc.
    for fmt in (None, "%I:%M %p on %d %B, %Y", "%Y/%m/%d %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %B, %Y", "%d %B %Y",
                "%I:%M %p on %d %b, %Y"):
        for cand in (cleaned, cleaned.upper()):
            try:
                if fmt is None:
                    return datetime.datetime.fromisoformat(cand).isoformat()
                return datetime.datetime.strptime(cand, fmt).isoformat()
            except Exception:
                continue
    return None

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "data")

# LOCOMO's integer categories -> human names (per the LoCoMo paper).
LOCOMO_CATEGORIES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}


def _load_json(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"dataset not found: {path}\n"
            f"  -> run: bash {os.path.join(_HERE, 'fetch_datasets.sh')}"
        )
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# LOCOMO
# ---------------------------------------------------------------------------

def load_locomo(path: Optional[str] = None) -> list[dict]:
    path = path or os.path.join(DATA_DIR, "locomo10.json")
    raw = _load_json(path)
    groups = []
    for i, item in enumerate(raw):
        conv = item.get("conversation") or {}
        gid = str(item.get("sample_id") or f"locomo_{i}")
        turns = []
        # sessions are keys session_1, session_2, ... with sibling *_date_time.
        sess_nums = sorted(
            int(k.split("_")[1]) for k in conv
            if k.startswith("session_") and k.split("_")[1].isdigit()
            and not k.endswith("date_time")
        )
        for n in sess_nums:
            date = conv.get(f"session_{n}_date_time")
            for turn in conv.get(f"session_{n}", []) or []:
                text = turn.get("text") or turn.get("clean_text") or ""
                cap = turn.get("blip_caption")
                if cap:
                    text = f"{text} [shared an image: {cap}]".strip()
                if not text:
                    continue
                turns.append({
                    "speaker": turn.get("speaker") or "user",
                    "text": text,
                    "timestamp": _to_iso(date),
                    "session": f"session_{n}",
                })
        questions = []
        for j, qa in enumerate(item.get("qa", []) or []):
            cat = qa.get("category")
            questions.append({
                "id": f"{gid}_q{j}",
                "category": LOCOMO_CATEGORIES.get(cat, f"cat{cat}"),
                "question": qa.get("question", ""),
                "answer": str(qa.get("answer", qa.get("adversarial_answer", ""))),
                "question_date": None,
            })
        if turns and questions:
            groups.append({"group_id": gid, "suite": "locomo",
                           "turns": turns, "questions": questions})
    return groups


# ---------------------------------------------------------------------------
# LongMemEval
# ---------------------------------------------------------------------------

def load_longmemeval(path: Optional[str] = None, variant: str = "s") -> list[dict]:
    path = path or os.path.join(DATA_DIR, f"longmemeval_{variant}.json")
    raw = _load_json(path)
    groups = []
    for i, item in enumerate(raw):
        qid = str(item.get("question_id") or f"lme_{i}")
        sessions = item.get("haystack_sessions") or []
        dates = item.get("haystack_dates") or []
        turns = []
        for s_idx, sess in enumerate(sessions):
            date = dates[s_idx] if s_idx < len(dates) else None
            for turn in sess or []:
                content = turn.get("content") or turn.get("text") or ""
                if not content:
                    continue
                turns.append({
                    "speaker": turn.get("role") or "user",
                    "text": content,
                    "timestamp": _to_iso(date),
                    "session": f"session_{s_idx}",
                })
        # `_abs` questions are abstention cases — the correct answer is that the
        # info isn't present. Keep them: they test that we DON'T hallucinate.
        cat = item.get("question_type") or "unknown"
        if qid.endswith("_abs"):
            cat = f"{cat}(abstention)"
        questions = [{
            "id": qid,
            "category": cat,
            "question": item.get("question", ""),
            "answer": str(item.get("answer", "")),
            "question_date": item.get("question_date"),
        }]
        if turns:
            groups.append({"group_id": qid, "suite": "longmemeval",
                           "turns": turns, "questions": questions})
    return groups


# ---------------------------------------------------------------------------
# Selection / sampling (deterministic — reproducible regression runs)
# ---------------------------------------------------------------------------

def select(groups: list[dict], per_category: Optional[int] = None,
           max_questions: Optional[int] = None) -> list[dict]:
    """Trim questions to a balanced, DETERMINISTIC subset for a quick real
    number. per_category caps questions per category; max_questions caps the
    total (still category-balanced by round-robin). Groups left with no
    selected question are dropped so we don't build empty banks."""
    if per_category is None and max_questions is None:
        return groups

    # index questions by category, preserving order
    by_cat: dict[str, list[tuple[int, int]]] = {}
    for gi, g in enumerate(groups):
        for qi, q in enumerate(g["questions"]):
            by_cat.setdefault(q["category"], []).append((gi, qi))

    chosen: set[tuple[int, int]] = set()
    if per_category is not None:
        for cat, refs in by_cat.items():
            for ref in refs[:per_category]:
                chosen.add(ref)
    else:
        chosen = {r for refs in by_cat.values() for r in refs}

    if max_questions is not None:
        # round-robin across categories until we hit the cap
        ordered: list[tuple[int, int]] = []
        cats = list(by_cat.keys())
        pointers = {c: 0 for c in cats}
        while len(ordered) < max_questions:
            progressed = False
            for c in cats:
                refs = by_cat[c]
                p = pointers[c]
                if p < len(refs):
                    if refs[p] in chosen or per_category is None:
                        ordered.append(refs[p])
                    pointers[c] = p + 1
                    progressed = True
                    if len(ordered) >= max_questions:
                        break
            if not progressed:
                break
        chosen = set(ordered[:max_questions])

    out = []
    for gi, g in enumerate(groups):
        qs = [q for qi, q in enumerate(g["questions"]) if (gi, qi) in chosen]
        if qs:
            out.append({**g, "questions": qs})
    return out


def load(suite: str, per_category: Optional[int] = None,
         max_questions: Optional[int] = None, variant: str = "s") -> list[dict]:
    if suite == "locomo":
        groups = load_locomo()
    elif suite == "longmemeval":
        groups = load_longmemeval(variant=variant)
    else:
        raise ValueError(f"unknown suite {suite!r} (use locomo|longmemeval)")
    return select(groups, per_category=per_category, max_questions=max_questions)
