---
name: checkpoint
description: Use mid-task when context is getting long, before any risky operation, when pausing work, or when handing off to another agent/session. Writes resumable state to a file so a fresh session — or a stranger — can pick up cold.
---

# /checkpoint — write state a stranger could resume from

Long Discord sessions and unattended runs both die mid-thought sometimes.
The three logged Discord-reply-tool incidents were all "long turn lost the
thread" failures — this is the mitigation.

## When to use

Context is getting long; you're about to do something risky (infra change,
brain mutation, a merge); you're pausing work for the day; or you're handing
off to another agent/session (Duke↔Frank, or a fresh instance of yourself
after a restart/compaction).

## The procedure

1. **Write state to a file** (a scratch file, or the venture/agent's own
   state file if one exists — Frank's `10-memory/state.md` already does
   this well): goal, done-with-evidence, in-flight, ruled-out (so it isn't
   re-tried), next 3 concrete actions.
2. **Include exact resume commands + absolute file paths.** "Continue the
   refactor" is not resumable; "run `pnpm test path/to/file.test.ts`, then
   fix the 2 failing assertions at lines 44 and 61" is.
3. **Flag unverified claims explicitly** — reuse the `session-close` skill's
   format: an explicit "UNVERIFIED: none" line if genuinely nothing is
   unverified, never a silent omission.
4. **Store the pointer in the brain**: `remember(content="checkpoint: <one
   line>", tags="handoff,<task-id>")` so a teammate (or you, later) can
   `recall()` their way back to it.
5. **Test it** — read your own checkpoint back as if you were a stranger
   with zero memory of this session. If it doesn't fully orient you, it's
   not done.

## Rules

- Absolute paths, exact commands — not descriptions of what to do.
- Ruled-out approaches get recorded too, or the next session re-derives
  and re-fails them (this is exactly what L-004's "2-failure, change
  approach" rule protects against, extended across session boundaries).
- The checkpoint file is the memory; your context is not — write as if
  this session ends the instant you finish writing.
