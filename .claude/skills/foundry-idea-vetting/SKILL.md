---
name: foundry-idea-vetting
description: >-
  Use to evaluate a new venture/product idea (the VERDICT gate). The gauntlet:
  constraint matrix FIRST, the Boss's three questions answered honestly, the
  founder battery, the kill-pattern scan. Output PROCEED / PARK / REJECT with
  rejected-alternative reasoning. PROCEED needs the Boss's greenlight.
---

# Idea vetting — the gauntlet

Run it adversarially (a critic's job is to KILL the idea; survivors get built).

## §1 Constraint matrix FIRST (L-001: a day's work died for skipping this)
| Constraint | Pass test |
|---|---|
| C1 no-entity | Can revenue reach a individual via MoR rails? Does ANY vendor/license/platform demand business registration? One "yes" → fails → PARK with revive-trigger, don't kill |
| C2 margin ≥70% | Model the FAT case (3× compute, 8% refunds, worst fees). Still ≥70%? |
| C3 distribution | Is there a self-labeling search moment? What EXACTLY does the buyer type, and what does that click cost? No credible answer = no venture |
| C4 solo | Zero standing human fulfillment? Surges automatable? |

## §2 The Boss's three questions (answered honestly, never flattered)
1. **Will people use it and pay for it?** Proven willingness-to-pay for the JOB (not the product) at any price today? Cite who pays what to whom.
2. **Is it novel enough?** Which ingredients exist, which combination doesn't, who's structurally locked out of copying (price floor, regulatory moat, attention blind spot)?
3. **Is it groundbreaking?** Usually NO — and that's fine. Record what's quietly radical (the cost collapse, the access unlock).

## §3 The founder battery
Why pay within 5 minutes of first contact (the panic-moment test)? Why now, not
3 years ago / in 3 years (the window test)? What kills it, and would we see it
coming (watchable risks only)? Would the Boss's own personas pay (a shrug = the
persona leaves)? Is the money path honest (C8)?

## §4 Kill-pattern scan (auto-reject signals)
Saturated panic (50 funded competitors → CAC war) · platform-API dependency as
kill switch · regulated without a credentialed shelter · fragmented rulebooks
(expertise doesn't amortize) · B2B sales cycle vs an ads mandate · no purchase
trigger (education-heavy CAC). Also check `docs/foundry-registry/archive.md`:
rejected before? Anything changed since the kill reason was recorded?

## §5 The verdict
Written, logged as a decision in `docs/foundry-registry/decisions.md`: the
one-line concept, why it beat the constraint matrix, the 2-4 nearest rejected
alternatives + their failure patterns, the riskiest assumption (which the
probe tests first), the revive/kill triggers. A REJECT or PARK also gets an
entry in `docs/foundry-registry/archive.md` (kill reason + revive trigger) so
the next vetting pass doesn't re-spend the cycle.
**PROCEED requires the Boss's greenlight — package it as ONE askable line.**
