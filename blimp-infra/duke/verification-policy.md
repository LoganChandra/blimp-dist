---
topic: verification-policy
audience: Duke + Hermes + Frank + the Boss
summary: Hard rule — no completion report to the Boss and no A2A task flipped complete without the verifier's evidence artifact, stored in blimp-brain with tag "evidence" + the task id.
last_updated: 2026-07-09
---

# Verification policy — evidence or it didn't happen

Why this exists: Hermes's webhook model has fabricated tool calls
(memory: verify-Hermes-claimed-actions) and that already burned delivery
once. An end-gate critic alone is the 2024 pattern; the fix is checks
declared **before** work starts (the task contract,
`blimp-infra/duke/task-contract-template.md`) and run by an independent
read-only agent (`.claude/agents/verifier.md`).

## The hard rule

> **No completion report goes to the Boss, and no A2A task is flipped to
> `completed`, without a PASS verdict from the verifier subagent, stored
> in blimp-brain as an evidence artifact.**

Concretely, ALL of these are gated on the evidence artifact existing:

- `mcp__blimp-brain__a2a_complete` / `a2a_reply` that marks a task done
- `mcp__blimp-brain__task_update` setting a task to done
- Any Discord message to the Boss claiming a task is finished
- Any Boss report (or client-facing report) whose metrics are stated as
  fact — the numbers must trace to a verifier PASS, not a hand-read query
- Publishing a data refresh as the live dataset — row counts and freshness
  timestamps verified before the data is declared current
- Any bulk mutation of the team brain / `duke_data` — row counts verified
  before AND after, so a destructive op cannot silently gut the store
  (anchor: the 2026-06-23 brain wipe, recovered only from the 3am backup)

"I ran the tests and they pass" in an A2A return body is a **claim**.
Only the verifier's own captured output is **evidence**. This applies
symmetrically: Duke's work gets verified before Duke reports it; Hermes's
returns get re-verified on Duke's side before Duke accepts them —
regardless of what verification output Hermes pasted.

## The flow

1. **Contract first.** Before work starts, write the 5-field contract.
   Field 5 (verification commands + expected results) is mandatory.
   No field 5 → no delegation, no start.
2. **Mid-loop checks.** On long tasks, run field 5 (or the subset that's
   meaningful early) mid-loop, not just at the end — catch drift while
   it's cheap.
3. **Completion → verifier.** Spawn the `verifier` subagent with the
   contract. It runs field 5 verbatim, fresh context, read-only, and
   returns the verdict block.
4. **PASS → store evidence, then report.**
   - `mcp__blimp-brain__remember(content=<full verdict block>,
     tags="evidence,task:<task id>", source="verifier",
     context="<one-line objective>")`
   - Then (and only then) flip the A2A task / task tracker / tell the Boss.
   - The completion report quotes one line:
     `verified: PASS — evidence in brain, tags evidence,task:<task id>`.
5. **FAIL → no completion.** Store the FAIL verdict the same way (failed
   evidence is still evidence), then either fix and re-verify, or bounce.
   - A2A tasks ride the P0.6 bounce budget: a completion rejected **twice**
     auto-transitions to `failed` + one digest line + an incident memory.
     Never livelock against a fabricating counterparty.
   - `UNVERIFIABLE` / `UNSAFE-CHECK` / `BROKEN-CHECK` verdicts mean the
     contract is the bug — fix the contract, not the verdict.

## Task ids (what goes after `task:`)

| Work type | Task id |
|---|---|
| A2A task (Duke ↔ Hermes) | the A2A `task_id` (UUID) |
| Tracked blimp-brain task | the task tracker id |
| Ad-hoc / untracked work | `adhoc-YYYY-MM-DD-<slug>` (Asia/Kuala_Lumpur date) |

Retrieval: `mcp__blimp-brain__recall("<task id>", tags="evidence")` — used
by digests, by the Boss's spot checks, and by the P1.8 eval suite.

## Scope

In scope — anything that claims a change in the world: files written, code
shipped, DB rows mutated, configs staged, drafts produced, services probed
as "healthy". Overnight-shift drafts, Hermes briefs, weekly Boss reports, and
data refreshes are emphatically in scope.

Out of scope — pure conversation: answering a question, giving an opinion,
recalling a fact. No world-change claim, no artifact needed.

## Non-negotiables inherited

- The verifier never fixes anything and never trusts pasted output —
  see its own prompt.
- Evidence storage uses the existing `remember()` tool; no new
  infrastructure, no new tables.
- This policy is enforced by discipline now and by the P1.2 hooks later;
  hook installation is rail-#8 territory (the Boss types the settings diff).
