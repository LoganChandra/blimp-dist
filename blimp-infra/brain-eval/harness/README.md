# Brain Bench — the standard-benchmark harness for the Blimp brain

The honest way to know how good our memory is. It runs the **real public
benchmarks** every serious memory system competes on — **LOCOMO** and
**LongMemEval** — against our actual brain, and gives us **our own measured
number** instead of a borrowed one.

Built to answer \"how do we know our 91.4% is real?\" The answer:
we didn't — that figure is Hindsight's published score, never measured on our
deployment. This harness fixes that, and re-running it after every brain change
turns self-improvement into something we can prove, not claim (C6).

## Why our own number matters

Published leaderboard scores are **not comparable across vendors** — the same
system (Zep) has scored 66% and 75% on LOCOMO depending on who ran the harness.
So a number you didn't produce is marketing. The only trustworthy score is one
**you** run, in **your** harness, against **your** brain. That is what this is.

## Safety

Every benchmark question runs in an **isolated, throwaway Hindsight bank** named
`bench_*`. `engine._guard()` is a hard assert that refuses to ingest into or
DELETE any bank not prefixed `bench_`, so the harness can **never** touch the
live `duke` team brain (the 2026-06-23 wipe is why this is enforced in code).

## Use

```bash
cd blimp-infra/brain-eval
./brain-bench fetch                              # download datasets (once)
./brain-bench run --suite locomo --per-category 3   # quick, balanced real number
./brain-bench run --suite locomo                 # full LOCOMO (~200 Q)
./brain-bench run --suite longmemeval --variant oracle --max 30
./brain-bench report results/brain-bench-locomo-latest.json   # HTML scorecard
```

- `--mode qa` (default): recall → answer from recalled context only → LLM-judge
  vs gold. The number comparable to published leaderboards.
- `--mode retrieval`: cheaper diagnostic — did recall surface the gold answer?
  Isolates the retriever from the answerer/judge. No answer-gen cost.
- `--k`: recall top-k (default 10). `--per-category N` / `--max N`: deterministic
  balanced sub-sample for a fast run. `--limit-groups N`: cap banks built
  (useful for LongMemEval's per-question haystacks).

## What it measures

- **Overall accuracy** and a **per-category breakdown** — LOCOMO's single-hop /
  multi-hop / temporal / open-domain / adversarial, LongMemEval's six question
  types. These categories are where you see *where* we're weak (bet: temporal +
  contradiction), not just an aggregate.
- **Retrieval-hit rate** (non-abstention): did the recalled context even contain
  the answer — separating a retriever miss from an answerer/judge miss.
- **Abstention** questions (LongMemEval `_abs`, LOCOMO adversarial) score correct
  only if the brain **refused to answer** — so confidently answering from noise
  counts against us. That is the honesty metric.
- Recall **latency** (p50/p95) and wall time.

## Files

| File | Role |
|---|---|
| `engine.py` | brain interface: isolated bank lifecycle (guarded), ingest, recall, answer-gen |
| `datasets.py` | load + normalise LOCOMO / LongMemEval into one grouped schema; deterministic sampling |
| `judge.py` | LLM-as-judge (fails closed → biases our number down, the honest direction) |
| `run_bench.py` | orchestrator + CLI + aggregation; writes `results/brain-bench-<suite>-*.json` |
| `report.py` | renders a results JSON into a self-contained HTML scorecard |
| `fetch_datasets.sh` | downloads the raw datasets into `data/` |
| `../brain-bench` | the wrapper all three agents call |

## Regression use (the point)

`results/brain-bench-<suite>-latest.json` is a stable pointer. Run it before a
brain change, make the change, run it again, diff the two — every upgrade on the
roadmap (bitemporal resolver, contextual retrieval, trust weighting …) ships
with a real before/after number. Wire it into the weekly evals timer once the
first full baseline exists.

## Config (from `~/blimp/.env` or env)

`OPENROUTER_API_KEY` (required for `qa` mode), `HINDSIGHT_URL`
(default `http://localhost:8888`), `BRAIN_BENCH_ANSWER_MODEL`,
`BRAIN_BENCH_JUDGE_MODEL` (default `google/gemini-2.5-flash`).
