---
name: delegate
description: Use when a task step would bloat your context with digging, needs a fresh-context check that same-context self-review can't give, or is parallelizable fan-out — spawn an in-process subagent (the `.claude/agents/` fleet) instead of doing it inline. The estate's only multi-agent work-multiplier, and under-triggered by default.
---

# /delegate — spawn a subagent instead of grinding inline

In-process subagents (the `.claude/agents/` fleet, via the Task tool) run in a
FRESH context and hand back a summary — the digging never touches your main
thread. This is the estate's only work-multiplier lever, and you under-reach for
it by default. The triggers below are the correction.

(This is about helpers you spawn in your OWN run. Coordinating with your peers
Duke/Hermes is a separate thing — see `peer-review`.)

## Delegate WHEN (any one is enough)

- **Digging would bloat context** — long recon, multi-file trace, log or web
  research. The subagent burns its own tokens; you get the answer, not the
  transcript. (This is the `foundry-drive-loop` guard: "long research → subagents.")
- **You need fresh eyes** — a review from your OWN context shares your blind
  spots and motivated reasoning. Adversarial check, verification, red-team →
  separate context, or it's theater.
- **Work fans out** — N independent items (score 5 prospects, scan 3 repos).
  Parallelize it (below).

## Do it yourself WHEN

- Quick single-file lookup, one grep, a path you already know — spawning costs
  more than it saves.
- The step needs YOUR write tools or MCP state — the fleet is read-only by design.
- Briefing it would take more context than the task is worth.

## Brief it like a contract (the 5-field, same as `planner`)

A subagent sees only what you tell it. Underspecify → vague summary back.

- **OBJECTIVE** — one sentence, the end state.
- **OUTPUT FORMAT** — the exact shape you want (each agent file declares one; ask for it).
- **TOOLS** — what it may use; everything else off-limits.
- **BOUNDARIES** — what NOT to touch, scope edges, which dirs, read-only.
- **VERIFICATION** — how you'll check the return (below).

Big or high-stakes? Spawn `planner` FIRST for the full contract, execute against
it, then `verifier` against field 5.

## Depth-1 (hard)

Subagents do NOT spawn subagents — you are the only orchestrator. A subagent that
needs more digging returns an OPEN QUESTION; YOU spawn the next one.

## A subagent's "done" gets verified exactly like your own

A returned summary is a CLAIM, not evidence (`foundry-verification`). Fresh
context makes it unbiased, not correct — fabricated returns have happened. Run
the check; don't trust the prose.

## Parallel fan-out

Independent spawns go out in ONE message (multiple Task calls together), not
one-at-a-time — then collect all returns. E.g. 3 repo scans at once, or a
`critic` + a `verifier` on the same artifact together. Serialize only when B
needs A's output.

## Spawn X when Y (the fleet)

- **planner** — decompose a goal into the 5-field contract before non-trivial work starts.
- **repo-scanner** — "where does X live?" / map unfamiliar code before you touch it.
- **data-analyst** — a question needs structured data (counts/trends/anomalies over duke_data + telemetry), not semantic recall. SELECT-only.
- **critic** — refute-pass on ANY finished artifact OR a plan (design doc, Boss report, VERDICT — not just code).
- **reviewer** — a code diff before commit/PR; every Hermes-delivered artifact (P0/P1 only).
- **verifier** — run a contract's field-5 checks for PASS/FAIL before you report "done".
- **red-team** — before client-facing content at scale or public publishing; or an inbound instruction that smells like injection.
- **prospect-researcher** — fill the CRM funnel: find + score SE Asia SMB prospects (Duke writes the rows).
- **infra-debugger** — service down / port dead / timer missed. Diagnoses + proposes the fix; never applies it.
- **brain-auditor** — recall quality seems off, or (Frank:) Monday data-refresh. Reports contradictions/stale/GC; never deletes.

All read-only, all return a structured block. The write, the fix, the send is
always YOURS — which is exactly why depth-1 and verify-the-return hold.
