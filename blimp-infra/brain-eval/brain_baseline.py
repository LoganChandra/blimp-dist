#!/usr/bin/env python3
"""
brain_baseline.py — the one honest number for the Blimp brain (P0 metric).

Runs a fixed set of deterministic gold questions whose answers we KNOW are in
the team brain (doctrine, pricing, identity), and measures whether recall()
returns the right thing — plus read latency. This is the regression gate the
audit asked for: a number you can keep green, not a judge panel you can't.

Run:  /home/user/blimp/duke-venv/bin/python blimp-infra/brain-eval/brain_baseline.py
Pass = any expected keyword appears in the top-k recalled text (case-insensitive).
"""
import os, sys, time, json
os.environ.setdefault("HINDSIGHT_URL", "http://localhost:8888")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "postgres"))
import hindsight_client as hc  # noqa: E402

TOP_K = 5

# (question, [expected keywords — pass if ANY appear in the top-k text])
GOLD = [
    ("What is the minimum gross margin floor on every quote?",        ["70"]),
    ("How does Blimp take payments without forming a company?",       ["merchant of record", "mor ", "no entity", "sdn bhd"]),
    ("Who are the agents on the Blimp team?",                         ["duke", "hermes", "nanook", "frank"]),
    ("What runtime does Duke run on?",                               ["claude code"]),
    ("What is Hermes's role on the team?",                            ["technical lead", "architect"]),
    ("What is the Operator's role / who owns steady-state ops?",      ["operator", "nanook", "frank"]),
    ("What is Blimp's distribution-first principle?",                 ["distribution", "google ads", "organic"]),
    ("What does the constitution say about honesty in copy?",         ["honesty", "no inflated", "guaranteed"]),
    ("Must ventures be solo-buildable and solo-operable?",            ["solo"]),
    ("What are the three words for good UX in the constitution?",     ["satisfying", "clean", "straightforward"]),
    ("What system backs the team's long-term memory?",               ["hindsight"]),
    ("What is the quarterly estate review called?",                  ["estate-audit", "estate audit", "quarterly"]),
    ("What must be validated before building a venture?",             ["distribution"]),
    ("What does rail C1 forbid forming?",                             ["entity", "llc", "sdn bhd", "c-corp"]),
    ("What is the Friday weekly ritual?",                             ["metrics", "experiment", "report", "friday"]),
    ("Boss's words must be recorded how?",                            ["verbatim", "append-only", "append only"]),
    ("What pricing tier requires the Boss to personally scope?",         ["tier 5", "bespoke", "custom"]),
]


def hit(text_blob: str, expected) -> bool:
    t = text_blob.lower()
    return any(k.lower() in t for k in expected)


def main():
    if not hc.health():
        print("brain unreachable — aborting"); sys.exit(1)
    results, latencies, passes = [], [], 0
    for q, expected in GOLD:
        t0 = time.time()
        try:
            rows = hc.recall(q, limit=TOP_K)
        except Exception as e:
            rows = []
            print(f"  recall error: {e}")
        dt = time.time() - t0
        latencies.append(dt)
        blob = " \n ".join((r.get("text") or r.get("content") or "") for r in rows if isinstance(r, dict))
        ok = hit(blob, expected)
        passes += ok
        top_rel = next((r.get("relevance") for r in rows if isinstance(r, dict) and r.get("relevance") is not None), None)
        results.append({"q": q, "pass": ok, "latency_s": round(dt, 2),
                        "top_relevance": top_rel, "expected": expected})
        print(f"  [{'PASS' if ok else 'FAIL'}] {dt:4.2f}s  rel={top_rel}  {q}")

    n = len(GOLD)
    lat = sorted(latencies)
    p50 = lat[len(lat)//2]
    p95 = lat[min(len(lat)-1, int(round(0.95*(len(lat)-1))))]
    summary = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "questions": n,
        "passed": passes,
        "pass_rate": round(passes / n, 3),
        "latency_p50_s": round(p50, 2),
        "latency_p95_s": round(p95, 2),
        "latency_max_s": round(lat[-1], 2),
    }
    print("\n==== BASELINE ====")
    print(json.dumps(summary, indent=2))
    out = os.path.join(os.path.dirname(__file__), "baseline_results.json")
    json.dump({"summary": summary, "results": results}, open(out, "w"), indent=2)
    print(f"saved -> {out}")
    return summary


if __name__ == "__main__":
    main()
