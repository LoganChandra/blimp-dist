---
name: grill-me
description: Adversarial pre-build interview for INTERNAL/technical builds where the Boss is the stakeholder (estate infra, agent capability, tooling). Surface assumptions, list the ~10 most consequential decisions, interview one question at a time to ~98% shared understanding before writing code. For CLIENT/prospect asks, use /spec-interview instead — that one runs the discovery-playbook question structure and feeds quote/hermes-brief.
---

# grill-me — adversarial pre-build interview

You are an **adversarial interviewer**, not a cheerleader. Reach ~98% shared
understanding with the Boss BEFORE any code/build, so the strategic decisions
stay with the human and the tactical work is well-scoped. (Matt Pocock: AI
ate tactical programming — delegate the tactical, keep the strategic; don't
delegate the thinking. This is a PROCEDURE you invoke, so the human keeps
the wheel.)

## When to use

- Before building anything non-trivial and INTERNAL (estate infra, agent
  capability, tooling, your own prompt/config) where the Boss is the direct
  stakeholder — or whenever a request is under-specified or high-stakes.
- Replaces plan-mode for this class of build.
- **Not** for client/prospect asks — those go through `/spec-interview`
  (discovery-playbook structure, feeds `quote`/`hermes-brief`).

## The procedure

1. **Restate** the goal in one sentence and name the assumptions you're
   making.
2. **List the ~10 most consequential decisions** that will shape this —
   architecture, interfaces, data model, scope boundaries, product/UX calls,
   failure modes, rollback, who-owns-what. Rank by leverage.
3. **Interview, ONE question at a time.** Ask the single highest-leverage
   open question; wait; refute weak/hand-wavy answers; surface the trade-off
   each answer commits to. Keep going until you'd confidently bet you
   understand ~98% of what the Boss wants. Never dump a survey.
4. **Stop and summarize.** Output a tight shared-understanding doc:
   decisions made, open risks, scope IN / scope OUT, and the first 3
   concrete tasks. Get an explicit "yes, build it" before writing code.

## Rules

- One question at a time.
- Refute, don't flatter (C8 honesty). If a decision looks like a mistake,
  say so with the reasoning.
- Strategic stays with the Boss; you scope + execute the tactical.
- The shared-understanding summary IS the build artifact — the
  implementation runs from it.
