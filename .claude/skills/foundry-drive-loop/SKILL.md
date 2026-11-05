---
name: foundry-drive-loop
description: >-
  Use for unattended, autonomous, or multi-step work runs — the 7-step
  self-prompting loop that keeps an agent productive for hours without drifting,
  stalling, or hallucinating progress. Load when told "keep working / I'm gone /
  stay agentic," or whenever a task spans many steps.
---

# The drive loop — autonomous work protocol

Run literally, every iteration:

1. **ORIENT** — read `docs/foundry-registry/registry.md` (the estate registry)
   + active state files (Frank's `10-memory/state.md`, or the venture's own
   state file). What state is each thing in? What gates are unmet? What's
   blocked?
2. **SELECT** — pick the SINGLE highest-leverage unblocked action via the
   leverage test (below). Write a one-line intent FIRST: "I will X so gate Y
   advances."
3. **EXECUTE** — scope-boxed. Subtasks that sprout go to the state-file queue,
   not chased.
4. **VERIFY** — run the strongest available check; record command + literal
   result. No check available → build one, or label the work `unverified`
   (unverified work cannot satisfy a gate). For contracted work, spawn the
   `verifier` subagent (`.claude/agents/verifier.md`) at completion to run the
   contract's field-5 checks fresh and read-only — no PASS, no completion
   (`blimp-infra/duke/verification-policy.md`).
5. **RETRO** — one line minimum: worked / failed / lesson. Failures ALWAYS
   produce a lesson candidate.
6. **COMMIT** — update state files + `docs/foundry-registry/registry.md`
   before continuing. The FILES are the memory; context is not. Any fresh
   session must be able to resume from files alone.
7. **RE-PROMPT** — write the next iteration's own prompt from the NEW state
   (not from memory of the old plan). Loop.

## The leverage test (step 2 tiebreaker — highest wins)
- Unblocks a gate on a venture closest to revenue: **+3**
- Compounds (SEO pages, lessons, playbooks, tests): **+2**
- De-risks the riskiest current assumption: **+2**
- Boss-queue prep (package a decision into one askable line): **+1**
- Cosmetic / refactor / nice-to-have: **+0** (only when nothing above exists)

Blocked lanes are skipped, not waited on.

## Drift guards (binding)
- **2 failed identical attempts → change approach or skip the lane.** Never brute-force (L-004).
- **Blocked on a Boss-only decision → write an escalation + move to the next lane.** Zero idle time.
- **Long research → fresh-context subagents**, summaries return (protects the main context).
- **Every ~5 iterations → checkpoint verification** of any touched codebase (tests + build).

## Authority while unattended (UNCHANGED by "full access")
No payments, no KYC/account creation, no pricing changes, no deletions, no
external commitments — package those as escalations. "Access ≠ authorization."

## Exit
Write a run report into the state file (iterations, gates advanced, evidence
index, lessons appended, Boss-ask queue) and leave a **RESUME PROMPT** so the
next session starts mid-stride. The estate is never left mid-thought.
