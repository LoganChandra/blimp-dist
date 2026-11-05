---
name: simplify-first
description: Use BEFORE writing any new code, component, or dependency — a 6-rung ladder that stops at the first option that holds (does this need to exist? already in the codebase? stdlib? native platform feature? installed dependency? one line?) before reaching for something bigger. Benchmarked externally at ~54% less code and ~20% cheaper with zero safety regression (source — DietrichGebert/ponytail, adopted 2026-07-08 into our own doctrine).
---

# /simplify-first — stop at the first rung that holds

The estate already has L-012 ("keep loaders thin") and L-011 ("static
instruction files are the floor, not the product") pointed at doctrine
bloat. This is the same discipline aimed at CODE: the default failure mode
of an agent given a task is to reach for the biggest tool that could
plausibly work (a new dependency, a new component, a new abstraction) when
something already sitting in the codebase — or a single line — does the
job.

## When to use

Before writing any new code: a component, a utility function, a dependency
addition, a config file. Not a replacement for `/decide` (which is about
judgment calls) or `/pre-mortem` (failure-imagination) — this is
specifically the "how much code does this actually need" check.

## The ladder — stop at the FIRST rung that holds

1. **Does this need to exist at all?** (YAGNI) — is the feature actually
   requested, or is it a plausible-sounding extra scope hitching a ride?
2. **Is it already in this codebase?** Grep before writing — a near-
   identical helper/component/pattern already existing and being ignored is
   the single most common form of unnecessary code.
3. **Does the standard library do it?** Reach for stdlib before a package.
4. **Does the native platform already do it?** (e.g. `<input type="date">`
   before installing a date-picker library, browser-native form validation
   before a validation library.)
5. **Does an already-installed dependency do it?** Before adding a new one,
   check what's already a dependency of this project.
6. **Can it be one line?** If yes, it's one line — not a wrapper, not a
   configurable options object, not a new file.

Only past rung 6 do you reach for a genuinely new abstraction/dependency —
and at that point it's earned, not defaulted to.

## The rule that keeps this safe

**This was never "fewest tokens" or "fewest lines" as the goal.** Write only
what the task needs, and NEVER cut validation, error handling, security, or
accessibility to hit a smaller diff — those aren't scope, they're
correctness. The code ends up small because it was necessary, not golfed.
If a rung would require dropping a real safety/correctness requirement to
stay small, that rung doesn't hold — move to the next one that keeps the
requirement intact.

## Why this is worth a dedicated skill, not just a CLAUDE.md line

Externally benchmarked (real agentic session, `git diff`-scored, 12 feature
tickets against a real FastAPI+React repo, n=4): ~54% less code, ~22% fewer
tokens, ~20% cheaper, ~27% faster than the same agent with no skill — while
holding 100% on an adversarial safety tier (a bare "write terse code" prompt
without this ladder dropped to 95% safe). The gap is biggest exactly where
an agent over-builds (a date picker ballooning to a full component +
dependency + stylesheet) and near-zero where the code was already minimal —
i.e., it costs nothing when there's nothing to cut, and saves real money
when there is.
