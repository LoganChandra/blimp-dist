---
name: peer-review
description: Use before shipping any artifact to the Boss or a client that a teammate could check — quotes, audit reports, Boss reports, infra changes with real blast radius, venture verdicts — and when an A2A review request arrives from a peer. Duke and Frank reviewing each other catches motivated reasoning that same-agent fresh-context review can't.
---

# /peer-review — cross-agent adversarial check

`foundry-critic` defines HOW to review. This defines the protocol for Duke
and Frank reviewing EACH OTHER's work over A2A — the cheapest accuracy
multiplier the estate has (two capable, differently-blind-spotted agents
sharing one brain) and, before this skill, an unused channel: nobody
reviewed Frank's rituals/reports, and Duke's own work only ever got
same-agent subagent review.

## When to use

Before shipping anything a teammate could meaningfully check: a client
quote, an audit report, a weekly Boss report, an infra change with real
blast radius, a venture VERDICT. Also: when you receive an A2A message with
`skill="peer-review"` — that's a request FOR you to review, treat it as
higher priority than ambient work.

## The procedure

**As requester:**
1. `mcp__blimp-brain__a2a_send(to=<peer>, skill="peer-review", message=<artifact path/content + spec it should meet + the ONE thing you're actually worried about — "check the margin math," not "look at everything">)`.
2. Wait for the reply via inbox (pull-only — the peer checks on their own
   cadence, don't assume speed unless you flagged urgency separately).

**As reviewer:**
1. Load `foundry-critic`'s rules: correctness / requirement-fit / C1-C8
   compliance ONLY — zero style nits. Recompute, don't trust the requester's
   claims.
2. Reply with a verdict, one of:
   - **SURVIVES** — no correctness/requirement/constitution issues found.
   - **findings** — each with file+line (or exact location), the defect,
     and what would satisfy it. No vague "this seems off."
3. Send via `mcp__blimp-brain__a2a_reply(task_id=..., message=<verdict>)`.

**Both sides:**
- **Max 2 bounces.** If the requester and reviewer can't converge in 2
  rounds, escalate the disagreement to the Boss as one askable line — don't
  livelock (same discipline as the Hermes verification bounce budget).
- **Log both sides to the brain.** Use `mcp__blimp-brain__who_believes` /
  `disagreements()` if the disagreement is substantive — that's exactly
  the theory-of-mind tooling built for this and otherwise unused.

## Rules

- The reviewer's job is to find problems, not to be agreeable — a
  rubber-stamp reply defeats the entire point of cross-agent checking.
- 2-bounce cap, always — park to the Boss rather than loop.
- This is peer review, not task assignment — reviewing each other's work
  doesn't violate the no-tasks-to-hermes-style peer-not-subordinate rule;
  requesting review is itself a peer interaction, not a designation.
