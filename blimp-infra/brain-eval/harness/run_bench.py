#!/usr/bin/env python3
"""
run_bench.py — run a public memory benchmark against the live Blimp brain and
produce OUR OWN measured score (not a borrowed leaderboard number).

For each group: build an isolated throwaway bank, ingest its conversation,
then for each question recall() as Duke would, generate an answer from ONLY the
recalled context, and judge it against gold. Aggregate by category + overall.

Usage:
  run_bench.py --suite locomo   --per-category 3
  run_bench.py --suite longmemeval --variant s --max 30
  run_bench.py --suite locomo --mode retrieval    # skip answer-gen, just measure recall hits

Modes:
  qa         (default) end-to-end: recall -> answer -> judge vs gold. The number
             comparable to published leaderboards.
  retrieval  cheaper diagnostic: did the recalled context contain the gold
             answer? Isolates the retriever from the answerer/judge.

Results land in blimp-infra/brain-eval/results/brain-bench-<suite>-<ts>.json.
Never touches the live `duke` bank (see engine._guard).
"""
from __future__ import annotations

import os
import sys
import json
import time
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import engine    # noqa: E402
import datasets  # noqa: E402
import judge as judgemod  # noqa: E402

RESULTS_DIR = os.path.join(_HERE, "..", "results")


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


def retrieval_hit(gold: str, context: str) -> bool:
    """Loose diagnostic: does the recalled context contain the gold answer?
    Whole gold string, else all-tokens-present for short answers."""
    g, c = _norm(gold), _norm(context)
    if not g:
        return False
    if g in c:
        return True
    toks = [t for t in g.split() if len(t) > 2]
    if toks and len(toks) <= 6:
        return all(t in c for t in toks)
    return False


def run(suite: str, per_category=None, max_questions=None, k=10,
        mode="qa", variant="s", keep_banks=False, index_wait=90.0,
        limit_groups=None, cooldown=0.0, qa_delay=0.0):
    if not engine.health():
        print("brain unreachable at", os.environ.get("HINDSIGHT_URL"), file=sys.stderr)
        sys.exit(1)

    groups = datasets.load(suite, per_category=per_category,
                           max_questions=max_questions, variant=variant)
    if limit_groups:
        groups = groups[:limit_groups]
    n_q = sum(len(g["questions"]) for g in groups)
    print(f"loaded {len(groups)} groups / {n_q} questions from {suite} "
          f"(mode={mode}, k={k})")

    items, recall_latencies = [], []
    errors = 0        # group-level (bank/ingest) failures
    q_errors = 0      # per-question failures (isolated — don't abort the group)
    t_start = time.time()

    for gi, g in enumerate(groups):
        label = f"{suite}{gi}"
        print(f"[{gi + 1}/{len(groups)}] group {g['group_id']} "
              f"({len(g['turns'])} turns, {len(g['questions'])} q)")
        try:
            with engine.isolated_bank(label=label, keep=keep_banks) as bank:
                posted = engine.ingest_turns(bank, g["turns"])
                engine.wait_until_indexed(bank, expect_min=max(1, posted // 4),
                                          timeout_s=index_wait)
                if cooldown:
                    # Let the reranker's per-minute rate window reset after the
                    # ingest burst before we start querying (Cohere throttles).
                    print(f"    cooldown {cooldown}s (reranker rate window)...")
                    time.sleep(cooldown)
                for qi, q in enumerate(g["questions"]):
                    try:
                        t0 = time.time()
                        rows = engine.recall(bank, q["question"], k=k)
                        dt = time.time() - t0
                        recall_latencies.append(dt)
                        ctx = engine.context_text(rows)
                        rec = {
                            "id": q["id"], "category": q["category"],
                            "question": q["question"], "gold": q["answer"],
                            "recall_n": len(rows),
                            "recall_latency_s": round(dt, 2),
                            "recall_hit": retrieval_hit(q["answer"], ctx),
                        }
                        if mode == "qa":
                            ans = engine.generate_answer(
                                q["question"], ctx, q.get("question_date"))
                            v = judgemod.judge(q["question"], q["answer"], ans,
                                               q["category"])
                            rec["answer"] = ans
                            rec["correct"] = bool(v["correct"])
                            rec["reason"] = v["reason"]
                        else:
                            rec["correct"] = rec["recall_hit"]
                            rec["reason"] = "gold in recalled context" \
                                if rec["recall_hit"] else "gold not retrieved"
                        items.append(rec)
                        mark = "OK " if rec["correct"] else "MISS"
                        print(f"    [{mark}] ({q['category']}) {q['question'][:60]}")
                    except Exception as qe:
                        # One question's failure (e.g. a persistent rate-limit)
                        # must NOT discard the whole group's results.
                        q_errors += 1
                        print(f"    ! q failed ({q['category']}): {str(qe)[:90]}",
                              file=sys.stderr)
                    if qa_delay and qi < len(g["questions"]) - 1:
                        time.sleep(qa_delay)
        except Exception as e:
            errors += 1
            print(f"    ! group failed (bank/ingest): {str(e)[:120]}", file=sys.stderr)
            continue

    summary = aggregate(items, suite, mode, k, recall_latencies,
                        groups_run=len(groups), errors=errors,
                        wall_s=time.time() - t_start, q_errors=q_errors)
    out = save(summary, items, suite)
    print_summary(summary)
    print("saved ->", out)
    return summary, out


def aggregate(items, suite, mode, k, latencies, groups_run, errors, wall_s,
              q_errors=0):
    by_cat: dict[str, dict] = {}
    correct = 0
    hits = 0
    non_abstain = 0
    for r in items:
        cat = r["category"]
        c = by_cat.setdefault(cat, {"correct": 0, "total": 0, "recall_hits": 0})
        c["total"] += 1
        c["correct"] += int(r["correct"])
        c["recall_hits"] += int(r.get("recall_hit", False))
        correct += int(r["correct"])
        if not judgemod.is_abstention_question(cat, r["gold"]):
            non_abstain += 1
            hits += int(r.get("recall_hit", False))
    for c in by_cat.values():
        c["accuracy"] = round(c["correct"] / c["total"], 3) if c["total"] else None
    total = len(items)
    lat = sorted(latencies) or [0]
    return {
        "meta": {
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "suite": suite, "mode": mode, "k": k,
            "answer_model": engine._ANSWER_MODEL,
            "judge_model": judgemod.JUDGE_MODEL,
            "groups_run": groups_run, "errors": errors,
            "question_errors": q_errors,
            "wall_seconds": round(wall_s, 1),
        },
        "overall": {
            "questions": total,
            "correct": correct,
            "accuracy": round(correct / total, 3) if total else None,
            "retrieval_hit_rate_nonabstain":
                round(hits / non_abstain, 3) if non_abstain else None,
        },
        "by_category": dict(sorted(by_cat.items())),
        "latency": {
            "recall_p50_s": round(lat[len(lat) // 2], 2),
            "recall_p95_s": round(lat[min(len(lat) - 1, int(0.95 * (len(lat) - 1)))], 2),
            "recall_max_s": round(lat[-1], 2),
        },
    }


def save(summary, items, suite):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(RESULTS_DIR, f"brain-bench-{suite}-{ts}.json")
    with open(out, "w") as f:
        json.dump({"summary": summary, "items": items}, f, indent=2)
    # also keep a stable "latest" pointer per suite for regression diffing
    latest = os.path.join(RESULTS_DIR, f"brain-bench-{suite}-latest.json")
    with open(latest, "w") as f:
        json.dump({"summary": summary, "items": items}, f, indent=2)
    return out


def print_summary(summary):
    o, m = summary["overall"], summary["meta"]
    print("\n==== BRAIN BENCH ====")
    print(f"  suite={m['suite']} mode={m['mode']} k={m['k']}  "
          f"answer={m['answer_model']}  judge={m['judge_model']}")
    print(f"  questions={o['questions']}  correct={o['correct']}  "
          f"ACCURACY={o['accuracy']}")
    if o.get("retrieval_hit_rate_nonabstain") is not None:
        print(f"  retrieval-hit (non-abstention)={o['retrieval_hit_rate_nonabstain']}")
    print("  by category:")
    for cat, c in summary["by_category"].items():
        print(f"    {cat:34s} {c['correct']:>3}/{c['total']:<3}  acc={c['accuracy']}")
    print(f"  recall latency p50={summary['latency']['recall_p50_s']}s "
          f"p95={summary['latency']['recall_p95_s']}s")
    if m["errors"]:
        print(f"  NOTE: {m['errors']} group(s) errored and were skipped.")


def main():
    ap = argparse.ArgumentParser(description="Run a public memory benchmark against the Blimp brain.")
    ap.add_argument("--suite", required=True, choices=["locomo", "longmemeval"])
    ap.add_argument("--per-category", type=int, default=None,
                    help="cap questions per category (balanced quick run)")
    ap.add_argument("--max", type=int, default=None, dest="max_questions",
                    help="cap total questions (category round-robin)")
    ap.add_argument("--limit-groups", type=int, default=None,
                    help="cap number of groups/banks built (useful for longmemeval)")
    ap.add_argument("--k", type=int, default=10, help="recall top-k")
    ap.add_argument("--mode", choices=["qa", "retrieval"], default="qa")
    ap.add_argument("--variant", default="s", help="longmemeval variant: s|oracle|m")
    ap.add_argument("--index-wait", type=float, default=90.0,
                    help="seconds to wait for server-side indexing per bank")
    ap.add_argument("--keep-banks", action="store_true",
                    help="don't delete throwaway banks (debug)")
    ap.add_argument("--cooldown", type=float, default=0.0,
                    help="seconds to wait AFTER ingest before QA (lets the "
                         "reranker rate window reset; try 60 if rate-limited)")
    ap.add_argument("--qa-delay", type=float, default=0.0,
                    help="seconds between questions (pace under the reranker "
                         "rate limit; try 8)")
    args = ap.parse_args()
    run(args.suite, per_category=args.per_category, max_questions=args.max_questions,
        k=args.k, mode=args.mode, variant=args.variant, keep_banks=args.keep_banks,
        index_wait=args.index_wait, limit_groups=args.limit_groups,
        cooldown=args.cooldown, qa_delay=args.qa_delay)


if __name__ == "__main__":
    main()
