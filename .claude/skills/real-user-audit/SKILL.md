---
name: real-user-audit
description: Walk a product the way its least technical real user would, phone first, then research and fix each place they would trip, one question at a time, with no simplifying or assuming. Use it before shipping any screen people will use, whenever the Boss says "put yourself in the shoes of…", "audit it", or "what would Apple do?", and as the default way to finish any UI work.
topic: Real-user audit (research every turn, never simplify or assume)
audience: Frank, Duke, Hermes
summary: Persona walk, a researched fix per trip-up, adversarial checks, fresh-context reviews until clean, an honest report. First run on <your-product> Projects, 13 Oct 2026.
last_updated: 2026-10-13
---

# Real-user audit

the Boss, verbatim (2026-10-12/13):
- *"put your shoes in a really non techinical HR manager. who barely knows how a browser works. audit it, research at every iteration and make changes as you go. rmb. at the end, mobile first and make sure to use every skill and every lesson we've learned."*
- *"what would apple do?"*
- *"thats not enough. research at every turn. never simplify or assume. turn it into a skill and apply it to your soul and everyones soul"*

## The three rules (they outrank speed)

1. **Research at every turn.** Every fix, every claim and every "that's how it works" gets a source first: a primary doc, a standard, a study or a real product. Say which parts are search-backed and which are your own reasoning. If search fails, say so.
2. **Never simplify.** Don't swap the person's real problem for an easier one, don't cut the hard path, and don't call a partial fix done.
3. **Never assume.** Unverified means unverified. Write "not tried on a real iPhone", not "works on iOS". If you are unsure what the person means, ask the Boss, or build the product to ask the user.

## The method

1. **Name the person.** Use the real first user: their job, age, skills, device and where they are. The digital inclusion scale (GOV.UK service manual) helps place them, and you should design for its lower end. For <your-product>, the first users are the Boss's parents, who worked at the dawn of the internet, and HR managers like them. Say which parts of the persona come from the Boss and which are your own working assumption.
2. **Use their device.** Look up the market's real screen sizes (StatCounter, by country) and walk at the two commonest phone sizes first. For example 414x896 and 360x800. Then check a laptop at 1366x768 and a monitor at 1920x1080. Launch Chromium with `--font-render-hinting=none` so the screenshots match what phones actually draw.
3. **Walk every screen as them, in order.** Use a script that clicks like they would and screenshots every screen (`tests/e2e/projects/hr-walk.mjs` is the template). Look at every shot yourself. At each step ask the four cognitive-walkthrough questions (Wharton et al., in Nielsen and Mack 1994):
   - Will they try to do the right thing?
   - Will they see the control for it?
   - Will they connect that control to what they want?
   - After doing it, will they see it worked?
   Then ask the Apple question: would Apple ship this screen?
4. **Write down every trip-up with evidence:** the screenshot, what they would think, what goes wrong. Include the people around them, such as the colleague receiving an invite and the signed-out visitor.
5. **Research each fix before writing it.** One question per trip-up, for example "does Chrome let a page set the date format?", "how long should an undo toast stay?", "is a drag-only reorder accessible?". Use Nielsen's 10 heuristics, WCAG 2.2, the Apple HIG and TipKit, NN/g, Baymard, GOV.UK and the platform docs. The fix follows the source, and the source goes in the code comment.
6. **Fix the class, not the instance.** One date box in mm/dd means every date box: make a shared component.
7. **Write the adversarial checks before saying done** (`tests/e2e/projects/hr.mjs` is the template). Cover phone and laptop, the hostile inputs ("yo", emoji, Malay, pasted newlines, empty), slow networks, Back, rotating the phone, two windows, and a colleague doing the same thing. State the count ("83/83"), never "it works".
8. **Re-run every old suite**, including the live-model ones. A live-model failure gets a rerun and is named as model variance only if the rerun passes.
9. **Get fresh-context review until clean.** Send the `reviewer` subagent a P0/P1-only brief that names the risky parts. Fix what it finds, add a test for each finding, and send the fixes back for review. Repeat until there are no P0s. On the first run, three passes found one P0 and six P1s that my own tests had missed, all in my own changes.
10. **Update the docs** (white paper, partner brief, PDFs) and the lessons: the brain, `references/13-saas-launch-playbook.md`, and this skill if the method changed.
11. **Report plainly.** Give what changed, the test counts, what the reviewers found, what was not verified, and the questions only the Boss can answer. Never rename his things, and never take a decision that is his.

## What the first run taught (<your-product> Projects, 13 Oct 2026)

- Dates go in words, never as numbers. A web page cannot set the date box's format.
- Undo beats a confirm question. A delete waiting on its Undo goes through only when the toast expires or the page is left, never when a section unmounts.
- An automatic "it's empty, delete it" must be checked by the server in the same statement, never decided from client state.
- Nothing may need dragging (WCAG 2.5.7).
- Full-screen editors on phones need Done, and Back must close them. Decide the layout once, when it opens.
- Share through the phone's Share sheet, and say what the recipient must do.
- Put one place to type on each screen.
- An unlabelled icon gets one tip, at most three times.
- Never say "drop" on a phone.

The full list is in `.claude/skills/claude-code-frontend/references/13-saas-launch-playbook.md`, section 11.

## Related

`claude-code-frontend` (house rules, including "what would Apple do"), `foundry-premium-ux` (the friction hunt), `foundry-verification` (V1 to V5), `peer-review`, `pre-mortem`.
