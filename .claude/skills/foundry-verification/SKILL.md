---
name: foundry-verification
description: >-
  Use before reporting ANYTHING "done," before trusting a teammate's claimed
  result, and at every gate. The V1-V5 check ladder, evidence-over-assertion,
  and confidence labeling. Core rule: "work without a runnable check is drafted,
  not done."
---

# Verification doctrine — the estate's epistemics

**Prime rule:** Work without a runnable check is not done — it is **drafted**.
Drafted work cannot satisfy a gate, cannot be reported complete, and must be
labeled `unverified` in state.

## The check ladder (strongest available wins)
| Level | Check | Use for |
|---|---|---|
| V1 | Deterministic suite — tests, build exit codes, linters | code |
| V2 | Scripted diff — output vs fixture, schema validation, registry audit | data, manifests, generated content |
| V3 | Adversarial subagent — fresh-context critic reviews the diff vs spec, correctness-only | specs, plans, judgment calls (→ `foundry-critic`) |
| V4 | Primary-source verification — claim traced to the authoritative publisher | any external fact (rates, statutes, pricing, market sizes). News tells you WHERE to look, never WHAT to ship |
| V5 | Human evidence — screenshots, measured funnel numbers, real purchases | UX claims, probe verdicts, user behavior |

**Contracted work:** the independent `verifier` subagent
(`.claude/agents/verifier.md`) runs this ladder against a task contract's
field-5 checks in fresh, read-only context — no PASS, no completion
(`blimp-infra/duke/verification-policy.md`).

## Evidence over assertion
Record the check run + the literal result + the date.
- ❌ "tests pass"
- ✅ "`npm test` → 30/30, exit 0, 2026-06-16 21:52"

State files keep an evidence index per gate. Reviewing evidence is faster than
re-running — and it works for runs nobody watched.

## Confidence labeling (estate-wide law)
Every number carries `confidence: high|medium|estimate` + `verify: true|false`.
Flagged numbers are re-verified against primary sources before any external use
(ads, product copy, financial decisions). Model knowledge is directionally
solid and precisely stale (L-002) — label accordingly.

## Calibration honesty
Any confidence shown to a user must be confidence MEASURED. HIGH must mean ≥95%
on real outcomes, or the gate tightens. No confidence theater — internal or
external.

## Teammate claims
Hermes's webhook can run on a cheap model that sometimes fabricates tool calls.
Verify a claimed action against ground truth (the file, the test output, the
deploy) before trusting "done."
