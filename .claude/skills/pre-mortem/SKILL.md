---
name: pre-mortem
description: Use before executing any plan that touches shared infra, live data, a client system, or more than an hour of build work. Assume it's 2 weeks later and the work failed — name 5 ways, rank by damage, mitigate the top 2, name the rollback. 10-minute cap.
---

# /pre-mortem — imagine the failure before you build it

The estate's recorded incidents (broken symlinks, a live brain wiped by an
untested DELETE, SSR-hidden content shipped as "done," a hung build re-run
into a worse state) are exactly the class a 10-minute pre-mortem catches
cheaply, before they cost an hour of recovery.

## When to use

Before executing any plan that touches: shared infrastructure, the live
brain or any production data, a client-facing system, or more than ~1 hour
of build work. Skip it for small reversible edits — this is for the plans
where being wrong is expensive.

## The procedure (10-minute cap — this is a gate, not a document)

1. **Write the failure headline** — "it broke because ___" — five times,
   covering at least one per layer: data, code, infra, human/process,
   assumption. Force yourself past the first obvious one.
2. **Rank by damage × likelihood.** Most headlines won't matter; find the
   two that do.
3. **For the top 2: add a mitigation or a tripwire check** to the actual
   plan — not a note to self, an actual step ("check X returns nonzero
   before proceeding" beats "be careful about X").
4. **Name the rollback path explicitly.** No rollback path = escalate
   before proceeding, don't just note the risk and continue.
5. **Output is 5 lines appended to the plan**, not a standalone document —
   if this is taking more than 10 minutes, you're writing a postmortem for
   a failure that hasn't happened yet; stop and just ship the top-2
   mitigations.

## Rules

- 10-minute cap, hard. This is a cheap gate, not a deliverable.
- A rollback-less one-way-door plan gets escalated, not pre-mortem'd and
  shipped anyway.
- Pairs with `/decide`'s one-way-door classification — a pre-mortem is what
  you do WHILE pre-registering a one-way door's success criteria.
