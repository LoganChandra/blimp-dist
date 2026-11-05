#!/usr/bin/env python3
"""
selftest.py — exercise the harness pipeline END-TO-END without a live brain.

The unit tests cover datasets/judge/engine pieces; this covers the wiring the
real run depends on — runner -> aggregate -> save JSON -> render HTML — by
mocking the brain (isolated bank, ingest, recall, answer) and the judge with
deterministic stubs. So we prove the RUN path works before spending real
tokens/banks on it.

Run:  python selftest.py
"""
from __future__ import annotations

import os
import sys
import json
import contextlib

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import engine        # noqa: E402
import datasets      # noqa: E402
import judge as judgemod  # noqa: E402
import run_bench     # noqa: E402
import report        # noqa: E402

FAILS = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILS.append(name)


# --- synthetic dataset: 2 groups, known gold, one abstention question ---------
def fake_load(suite, per_category=None, max_questions=None, variant="s"):
    return [
        {"group_id": "g1", "suite": suite,
         "turns": [{"speaker": "a", "text": "The meeting is on Tuesday.", "session": "s1"}],
         "questions": [
             {"id": "g1q1", "category": "single-hop",
              "question": "What day is the meeting?", "answer": "Tuesday",
              "question_date": None},
             {"id": "g1q2", "category": "adversarial",
              "question": "What color was the car?",
              "answer": "Not mentioned in the conversation", "question_date": None},
         ]},
        {"group_id": "g2", "suite": suite,
         "turns": [{"speaker": "b", "text": "The price is 1500 ringgit.", "session": "s1"}],
         "questions": [
             {"id": "g2q1", "category": "single-hop",
              "question": "What is the price?", "answer": "1500", "question_date": None},
         ]},
    ]


# --- mock the brain: recall returns the group's own turn text -----------------
_STORE = {}


@contextlib.contextmanager
def fake_bank(label="run", keep=False):
    yield "bench_fake"


def fake_ingest(bank, turns, batch=25):
    _STORE[bank] = " ".join(t["text"] for t in turns)
    return len(turns)


def fake_recall(bank, question, k=10):
    # return the ingested text as one row (so retrieval_hit works for real golds)
    return [{"text": _STORE.get(bank, ""), "relevance": 0.8}]


def fake_answer(question, context, question_date=None):
    # a dumb extractor: echo the context (judge stub decides correctness)
    return context


def fake_judge(question, gold, hypothesis, category=""):
    if judgemod.is_abstention_question(category, gold):
        return {"correct": judgemod.looks_abstained(hypothesis), "reason": "abstain-check"}
    return {"correct": gold.lower() in (hypothesis or "").lower(), "reason": "stub"}


def main():
    # install mocks
    datasets.load = fake_load
    engine.isolated_bank = fake_bank
    engine.ingest_turns = fake_ingest
    engine.wait_until_indexed = lambda *a, **k: True
    engine.recall = fake_recall
    engine.generate_answer = fake_answer
    engine.health = lambda: True
    judgemod.judge = fake_judge

    # retrieval mode (no answer-gen/judge): golds appear in recalled context
    summ_r, out_r = run_bench.run("locomo", mode="retrieval")
    check("retrieval run produced 3 questions", summ_r["overall"]["questions"] == 3)
    check("retrieval found the two real golds",
          summ_r["overall"]["retrieval_hit_rate_nonabstain"] == 1.0)
    check("by_category present", set(summ_r["by_category"]) >= {"single-hop", "adversarial"})
    check("results json written", os.path.exists(out_r))

    # qa mode: the 2 single-hop golds are echoed => correct; the abstention
    # question gets a non-abstaining echo => correctly scored a MISS. So 2/3,
    # which proves the abstention mechanism fires end-to-end.
    summ_q, out_q = run_bench.run("locomo", mode="qa")
    check("qa run scored all 3", summ_q["overall"]["questions"] == 3)
    check("qa accuracy is 2/3 (abstention miss caught end-to-end)",
          summ_q["overall"]["accuracy"] == round(2 / 3, 3))
    check("the abstention question was scored a miss (didn't abstain)",
          summ_q["by_category"]["adversarial"]["correct"] == 0)

    # report renders to HTML
    data = json.load(open(out_q))
    html = report.render(data)
    check("report renders HTML with a score", "%" in html and "Brain Bench" in html)
    check("report has no italic styling (house rule)", "font-style:italic" not in html)

    print(f"\n{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
