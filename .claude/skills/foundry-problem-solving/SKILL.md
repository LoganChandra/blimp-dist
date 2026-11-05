---
name: foundry-problem-solving
description: >-
  Load when stuck on a hard or ambiguous problem, debugging something that won't
  resolve, or deciding under uncertainty. The estate's distilled problem-solving
  discipline: frame → decompose → don't-brute-force → verify → distrust-your-own-
  "done" → fix-the-system. Synthesizes the drive-loop, verification doctrine,
  lessons L-001..L-015, the critic, and Nanook's strategic-judgment addendum.
---

# Problem solving — the estate's discipline for hard / complex issues

Not one source doc — distilled from the Foundry method + lessons + the critic + an
adversarial review pass. Run it loosely as a loop; order matters.

## 1. FRAME before you fix
- **Nail the constraints first** (L-001): surface what's actually fixed, allowed, and wanted BEFORE designing — or you'll brilliantly solve the *wrong* problem. A whole founding day died to skipping this.
- **Re-attribute the symptom to its true layer** before patching: symptoms usually surface in the wrong place. High refunds = a PRODUCT problem, not finance. Rising human-effort-per-case = a deterministic-core leak, not a hiring need. A flaky build output isn't always a code bug. Fix the wrong layer and the real defect compounds. Ask: "what layer does this actually live in?"

## 2. DECOMPOSE + scope-box
- Take ONE highest-leverage piece at a time (leverage test: unblocks-a-gate > compounds > de-risks > nice-to-have). Write a one-line intent first.
- When a piece sprouts sub-problems, QUEUE them — don't chase them. Hard problems get solved one bounded chunk at a time, not all at once.

## 3. Don't BRUTE-FORCE
- **2 failed identical attempts → change the approach** (L-004). Twice the same way = the approach is wrong, not the effort. Stop, write the failure down, attack from a different angle (or skip the lane).
- **Diagnose before you retry** (L-015): tell "broken" apart from "slow/stuck" and find the real cause first ("a build writing nothing for >60s is hung, not slow"). Don't re-run blindly.

## 4. VERIFY with a real check, not a feeling
- "Work without a runnable check is DRAFTED, not done." Use the strongest available check (tests → scripted diff → adversarial review → primary source → human evidence).
- Evidence over assertion: record the command + literal result + date, not "it works."
- Distrust stale knowledge (L-002): recall is "directionally solid, precisely stale" — verify specifics against the primary source; label confidence honestly.

## 5. DISTRUST your own "done"
- The critic lens: "the work was done by someone who wants to believe it's done — you don't." Refute your own answer; recompute independently; get fresh eyes (a second agent is cheap).
- **Self-flattery guard:** when a number comes out suspiciously good (margin / conversion / TAM / "it works now"), re-derive it OPENLY to catch motivated reasoning. Good news earns MORE scrutiny, not less.

## 6. DEGRADE honestly under uncertainty
- When correctness is in doubt, lower the confidence you SHOW immediately (before the slower real fix) — never bluff certainty. **Fix the gate, never the display.** Honest "I'm not sure" beats confident-and-wrong.

## 7. EXTERNALIZE state
- "Files are the memory, context is not." On anything long/complex, write down what you know, what you've ruled out, and where you are. Kills drift, context-rot, and re-solving the same dead end.

## 8. FIX THE SYSTEM, not just the instance
- After every solve-or-fail: retro → extract the generalized lesson → change the protocol/playbook so it can't recur. "A lesson without a verified behavior change is a hope." That's how you get *better* at solving, not just solve this one.

## The one-line version
Frame the real problem (and the right layer) → cut it into bounded pieces → never repeat a failing move → prove it with a check → attack your own answer → tell the truth about confidence → write down the state → upgrade the system.
