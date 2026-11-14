---
name: foundry-self-improvement
description: >-
  Use at the end of a work session and after any failure — how to turn
  experience into compounding lessons (the micro/session/estate retro loop, the
  graduation path) and the canonical lessons ledger at
  docs/foundry-registry/lessons.md. Self-improvement is measured behavior
  change, not stated intention.
---

# Self-improvement — the training loop (C6)

**A lesson without an amendment is a diary entry. A lesson without verified
application is a hope.** The loop only closes at `verified-applied`.

## The unit: a lesson (append-only, never deleted; supersede instead)
`L-NNN | date | trigger (what happened, with evidence) | rule (generalized) |
applied-to (which playbook/skill/constitution was amended, or "pending") |
verified-applied (date + evidence behavior changed, or "unverified")`

## Three speeds
- **MICRO** — every drive-loop iteration: one line, worked/failed/lesson. Failures ALWAYS yield a lesson candidate.
- **SESSION** — every run report: promote real patterns to lessons; amend the affected playbook/skill IN THE SAME SESSION.
- **ESTATE** — periodic audit: critic samples lessons for an anti-recidivism check (applied/violated per lesson, with file evidence). Lessons applied 3+ times graduate toward constitution; lessons proven wrong are superseded.

**To actually change an agent (skill, prompt, hook), load `self-evolve`:** one delta per lesson, proven by the free gate (`blimp-infra/evolve/gate.sh`) and pass^3 on the touched cases before it merges.

## Graduation path
observation (micro-retro) → lesson (evidence attached) → amendment
(playbook/skill edit) → verified-applied (next occurrence handled right) →
[if universal] doctrine / [if wrong] superseded with reasoning.

## What "training the agent" means here
The model doesn't fine-tune; the system around it does (behavior = model ×
context × protocol). Improve context (lessons compound into skills; keep loaders
THIN — bloated files get ignored, L-012) and protocol (gates tighten where
failures occurred).

## The lessons ledger (scan before significant work)
Canonical, append-only, lives at **`docs/foundry-registry/lessons.md`** — not
copied here (a copy is exactly the drift this skill exists to prevent; L-012
says keep loaders thin — no L-NNN ceiling number here either, for the same
reason; read the file for the current count). Add a new entry there — never
inline here — on any real failure or confirmed non-obvious
success.
