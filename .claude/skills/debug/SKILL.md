---
name: debug
description: Use when any test, build, service, or integration fails more than once, or when observed behavior contradicts expectation — before the second retry. Reproduce first, keep a hypothesis ledger, bisect the layer stack, instrument before guessing.
---

# /debug — tactics for when something is actually broken

`foundry-problem-solving` covers strategy (change approach after 2 failed
identical attempts — L-004 — diagnose before retrying). This covers the
tactics of the diagnosis itself. Several estate incidents trace to skipping
this: brute-force retries, trusting a stale log as evidence (L-014),
re-running a hung build without clearing state (L-015).

## When to use

Any test, build, service, or integration fails more than once, or behavior
contradicts what you expected — before you retry a second time.

## The procedure

1. **Reproduce deterministically, or downgrade the problem.** If you can't
   make it fail on demand, you're debugging "flaky" not "broken" —
   instrument first (add logging/tracing), don't guess at a fix for
   something you can't reliably trigger.
2. **Write a hypothesis ledger** in a scratch file: `hypothesis → the ONE
   discriminating test that would confirm/deny it → result`. This kills the
   waste of re-testing a dead end you already ruled out an hour ago and
   forgot.
3. **Bisect the layer stack** — input, code, dependency, environment,
   infra — starting with the cheapest discriminating probe, not the layer
   you suspect emotionally. "It must be the code" is often wrong; check
   what changed.
4. **Instrument before guessing.** Logs, with a freshness check (mtime) —
   per L-014, a stale log file is not evidence of anything current. If the
   log hasn't been touched in the timeframe that matters, the process isn't
   running the code path you think it is.
5. **After the fix:** add a regression check (a test that would have caught
   this) and write one lesson-candidate line — was this a new failure mode,
   or a repeat of something already in `docs/foundry-registry/lessons.md`
   that didn't get applied?

## Rules

- Never re-run a hung process/build without checking for stale procs and
  clearing cache first (L-015) — "nothing written in 60s" means hung, not
  slow.
- A hypothesis that failed its discriminating test is RULED OUT — write it
  down so it doesn't get re-tried after a context compaction or handoff.
- 2 failed identical attempts at the SAME hypothesis → you're not
  debugging, you're brute-forcing (L-004) — change layer or escalate.
