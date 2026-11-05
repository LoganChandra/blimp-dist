---
name: hermes-brief
description: Render the 5-field task contract format for a build the Boss has explicitly directed to Hermes. Use ONLY when the Boss names the build and says route it to Hermes — Duke does not self-initiate task assignments to Hermes (superseded 2026-06-16, see docs/foundry-registry/decisions.md D-003). For everything else, send Hermes a peer update via a2a_send, not a task.
disable-model-invocation: true
---

# /hermes-brief — the 5-field task contract

**Policy (2026-06-16, supersedes the old "always delegate" default):** Duke
never designates work to Hermes on his own initiative — peer updates only.
This contract format still applies the ONE time it's needed: when **the Boss
himself** names a build and directs it to Hermes. In that case the contract
is what makes "done" checkable instead of a claim (Hermes's webhook model has
fabricated tool calls before — memory: verify-hermes-claimed-actions).
Verification commands are declared **before** work starts.

If the Boss hasn't named a specific build for Hermes, don't reach for this —
send a peer update instead (`a2a_send`, FYI framing, no task/deadline
language).

## Step 1 — read the canonical template

The template lives at `blimp-infra/duke/task-contract-template.md`.
**Read it now — do not reproduce it from memory.** It defines the 5 fields:

1. **Objective** — one sentence, the end state, not the activity
2. **Output format** — exact artifact shape (file paths, schema, message)
3. **Tools** — what Hermes may use; everything else is off-limits
4. **Boundaries** — what NOT to touch (files, services, scope edges)
5. **Verification commands** — machine-checkable assertions that prove the
   end state; runnable by the verifier subagent without Hermes present

## Step 2 — fill it for this task

- Objective names the client/prospect/call it serves (standing demand test).
- Verification commands must be safe to run repeatedly and return
  unambiguous PASS/FAIL (exit codes, grep counts, curl status — not vibes).
- If you cannot write a verification command, the task is underspecified —
  run `/spec-interview` logic on yourself first.

## Step 3 — send via A2A (never manual Discord nudges)

`mcp__blimp-brain__a2a_send(to="hermes", message=<rendered contract>, skill=...)`

- Log the delegation: `mcp__blimp-brain__log_message(channel=..., speaker=<your agent key>, ...)`.
- Nudge rule (memory: hermes-nudge-after-send): if no `working` state within
  ~10 min, re-notify once via A2A.

## Step 4 — on completion, verify before trusting

- Spawn the `verifier` subagent with the contract's verification commands.
- No A2A task flips complete and no report reaches the Boss without the
  evidence artifact (command + output). FAIL → `a2a_pushback` with the
  failing check, max 2 bounces, then mark failed + digest line.
