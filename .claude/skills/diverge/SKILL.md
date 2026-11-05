---
name: diverge
description: Use BEFORE committing to an approach on any design, naming, architecture, pitch, pricing, or strategy question — whenever the first idea is about to get built. Generates real alternatives across forced frames before converging, so the estate doesn't ship the first plausible idea by default.
---

# /diverge — generate before you pick

Every other doctrine in this estate prunes (critic refutes, idea-vetting
kills, verification gates). Nothing generates. This is the estate's one
divergent protocol — use it whenever the first idea is about to become THE
idea.

## When to use

Design, naming, architecture, pricing, positioning, or strategy calls where
you're one idea deep and about to build it. Skip it for low-stakes or
clearly-single-answer work (a bug fix has one right fix, not ten).

## The procedure

1. **Restate the goal + hard constraints.** Constitution items (C1-C4) count
   as *generative* constraints, not just filters — "no entity" and "≥70%
   margin" shape what a good answer even looks like.
2. **Generate ≥7 candidates across 3 forced frames** — don't just brainstorm
   freely, force the angle:
   - **Invert the assumption** — what if the opposite of the obvious choice
     were true?
   - **Steal from an adjacent industry** — how does a different domain solve
     this class of problem?
   - **10x-cheaper/simpler version** — what would this look like with 1/10th
     the budget or complexity?
3. **No evaluation during generation.** Quantity gate before quality gate —
   judging an idea while generating kills the weird-but-right ones early.
4. **Converge.** Apply the leverage test / constraint matrix (whichever
   fits the domain) and keep 2-3 survivors, not one — a second-best option
   is often the fallback if the first hits a wall.
5. **Record the rejected alternatives with a one-line reason each.** This
   feeds `foundry-idea-vetting`'s rejected-alternative requirement and
   `docs/foundry-registry/decisions.md` / `archive.md` — a rejected idea
   with a reason is reusable; a silently-dropped one gets re-generated and
   re-rejected next time.

## Rules

- Quantity before quality — resist the urge to stop at 3 good-looking ideas.
- Each of the 3 frames must produce at least one candidate — if a frame
  produces nothing, that's information (this problem doesn't bend that way).
- Convergence needs a stated reason per survivor, not just a gut pick.
