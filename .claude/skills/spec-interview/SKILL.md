---
name: spec-interview
description: Turn a vague client ask into a concrete build spec using the discovery playbook's question structure. Use when a prospect/client request is underspecified — before quoting, before briefing Hermes, before scoping any build. Works live (the Boss on a call) or async (written ask).
---

# /spec-interview — vague ask → build spec

## Step 1 — read the playbook

`sales/03-discovery-playbook.md` — the 15 questions in 4 groups. This skill
reuses that structure outside the live 30-min call: against a written ask,
an intake form (`sales/07-intake-template.md`), or the Boss paraphrasing a
client. Recall first: `mcp__blimp-brain__recall` for anything already known
about this client.

## Step 2 — interrogate the ask along the 4 question groups

Answer each from the material given; everything unanswered becomes an
explicit open question — never invent facts (AGENTS.md).

- **Time-leak** — what manual work does this replace? hours/week? who does it?
  what gets re-typed between systems?
- **Money-leak** — missed inquiries? response latency? value of a missed
  customer? leads with no follow-up?
- **Knowledge-gap** — what answers live only in people's heads? onboarding time?
- **Decision** — what was tried before? team size/roles? current software
  (integration surface)? who approves? the ONE bottleneck with biggest impact?

## Step 3 — emit the build spec

```
BUILD SPEC — [Client] ([Country])
Problem: [one sentence, in dollars or hours]
Current process: [steps, with numbers — who, how long, how often]
Desired end state: [observable behavior, not technology]
Channels: [IG / WhatsApp / voice / email / web]
Integrations: [each system the AI reads/writes — count them, this prices]
Languages: [list — each beyond English is +30%]
Volume: [inquiries/day, docs/week — sizing input]
Constraints: [compliance, data residency, client's hard deadlines]
Success metric: [the number that proves it worked, measurable in week 1]
Out of scope: [explicitly excluded, scope-creep fence]
Open questions: [numbered — what we must ask before quoting]
```

Promise system performance, never business outcomes (AGENTS.md).

## Step 4 — route the output

- Scope clear + open questions empty → run `/quote`.
- Build going to Hermes → the spec feeds `/hermes-brief` (objective +
  boundaries come straight from it).
- 3+ distinct opportunities or murky scope → pitch the paid audit instead
  (Path B in the playbook), then `/audit-report` once sold.
- Save the spec: `mcp__blimp-brain__remember` with tags
  `spec,client,<client-slug>` and update the prospect's notes.
