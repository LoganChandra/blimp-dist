#!/usr/bin/env python3
"""
judge.py — LLM-as-judge for benchmark answers.

Given a question, the gold answer, and the answer our brain produced from
recalled context, decide correct/incorrect. This is the same grading method the
published leaderboards use (an LLM compares the hypothesis to the reference),
so our number is measured the way theirs are — with the honest caveat that a
judge you run yourself is only comparable to your OWN past runs.

Two honest design choices:
- ABSTENTION questions (LongMemEval `_abs`, LOCOMO adversarial): the correct
  answer is that the info isn't there. We score "correct" only if the brain
  abstained ("I don't know" / "not mentioned"), so retrieving noise and
  confidently answering counts AGAINST us — exactly the honesty we care about.
- Fail CLOSED: a judge/infra error scores the item incorrect, never a silent
  pass. That biases our reported number DOWN, which is the safe direction.
"""
from __future__ import annotations

import os
import re
import json
from typing import Optional

import httpx


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    try:
        for line in open(os.path.expanduser("~/blimp/.env")):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(key, default)


OR_KEY = _env("OPENROUTER_API_KEY")
# A sturdier judge than the cheapest tier; overridable. Kept separate from the
# ANSWER model so we don't have the same model grade its own output by default.
JUDGE_MODEL = _env("BRAIN_BENCH_JUDGE_MODEL", "google/gemini-2.5-flash")

_ABSTAIN_PAT = re.compile(
    r"\b(i (don'?t|do not) know|not (mentioned|stated|available|provided|in the "
    r"(memory|context|conversation))|no (information|mention|record)|"
    r"can'?t (tell|determine|find)|unclear|unknown|insufficient)\b",
    re.I,
)


def looks_abstained(answer: str) -> bool:
    return bool(_ABSTAIN_PAT.search(answer or ""))


def is_abstention_question(category: str, gold: str) -> bool:
    c = (category or "").lower()
    g = (gold or "").lower()
    return (
        "abstention" in c
        or "adversarial" in c
        or bool(_ABSTAIN_PAT.search(g))
    )


def judge(question: str, gold: str, hypothesis: str,
          category: str = "") -> dict:
    """Return {"correct": bool, "reason": str}."""
    # Abstention: decided locally, no LLM needed — did we (correctly) refuse?
    if is_abstention_question(category, gold):
        ok = looks_abstained(hypothesis)
        return {"correct": ok,
                "reason": "abstained as required" if ok
                else "should have abstained but answered"}

    if not OR_KEY:
        return {"correct": False, "reason": "no OPENROUTER_API_KEY — judge unavailable"}

    prompt = (
        "You are grading whether a model's answer matches the reference answer "
        "for a question about an earlier conversation. Be strict about facts "
        "(names, numbers, dates) but tolerant of wording, paraphrase, and extra "
        "correct detail. The model answer is correct if it conveys the same "
        "factual answer as the reference.\n\n"
        f"Question: {question}\n"
        f"Reference answer: {gold}\n"
        f"Model answer: {hypothesis or '(empty)'}\n\n"
        'Reply with ONLY a JSON object: {"correct": true|false, "reason": "<=15 words"}.'
    )
    try:
        r = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OR_KEY}",
                     "Content-Type": "application/json"},
            json={"model": JUDGE_MODEL, "temperature": 0, "max_tokens": 120,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        txt = r.json()["choices"][0]["message"]["content"] or ""
    except Exception as e:
        return {"correct": False, "reason": f"judge call failed: {str(e)[:60]}"}
    try:
        obj = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
        return {"correct": bool(obj.get("correct")),
                "reason": str(obj.get("reason", ""))[:120]}
    except Exception:
        # Last-resort parse; fail closed if we truly can't read a verdict.
        low = txt.lower()
        if '"correct": true' in low or '"correct":true' in low:
            return {"correct": True, "reason": txt[:80]}
        return {"correct": False, "reason": f"unparseable judge output: {txt[:50]!r}"}
