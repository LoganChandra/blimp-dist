---
name: self-evolve
description: >-
  Use when a lesson, a repeated mistake, or the Boss's correction should change how an agent behaves
  (a skill, prompt, system-context or hook edit) — for Duke, Frank or Hermes alike. Turns the lesson
  into one itemised delta, proves it with the free gates (blimp-infra/evolve/gate.sh) plus the
  touched behaviour cases at pass^3, and opens a PR only if it improves and regresses nothing.
  On demand only, never on a timer.
---

# Self-evolve: change yourself only on proof

the Boss, 2026-11-06: "i want yall to be truly self evolving too" and "is this gonna cost tokens every
time we merge? i dont want that". So the loop is: learn from real mistakes, change one thing, prove
it with free checks first, spend tokens only on the behaviour cases the change touches, merge only
when the numbers say so. All three agents run the same loop; Hermes runs it through his own
skill tooling, with the same gate script.

Why gate it: self-written skills without a check make agents slightly *worse* on average
(SkillsBench, 2026: curated skills +16 pp, self-generated -1.3 pp). A change that isn't measured is a
guess with a commit message.

## The loop (one lesson per pass)

1. **Pick the lesson.** It must be real and recent: `recall("mistake OR lesson OR correction", tags=...)`,
   `docs/foundry-registry/lessons.md`, or the Boss's words in this session. Write the trigger with evidence
   (what happened, when, the message or file).
2. **Write one delta.** The smallest edit that would have prevented it: a line in a skill, a rule in
   a system-context file, a regex in a hook. One lesson, one delta. If you can't name the file and
   the line, you don't have a delta yet.
3. **Add the test first.** A behaviour case in `evals/cases/<id>.yaml` that fails today for this
   mistake (prompt + must_include / must_not_include / judge_rubric). For a brain change, a question
   in `blimp-infra/brain-eval/exam/questions.json`. For code, a unit test.
4. **Run the free gate:** `bash blimp-infra/evolve/gate.sh main`. It runs the unit tests, the hooks
   and agent tests, a syntax check, the memory exam when brain code changed, and lists the behaviour
   cases the diff touches. Zero tokens. Red means stop.
5. **Run the touched behaviour cases**, and only those, at pass^3, before AND after the delta:
   `bash evals/pass-k.sh --changed main` (and `--agent frank` for Frank). This is the only step
   that costs tokens, and only when the agent or the Boss decides to run it.
6. **Decide by the numbers.** Merge only if the new case goes from failing to pass^3, and no touched
   case drops. Otherwise drop the delta and record why in the lesson.
7. **PR, then the ledger.** Branch + PR with the before/after numbers in the body. Append the lesson
   to `docs/foundry-registry/lessons.md` with `applied-to` = the file and `verified-applied` = the
   pass^3 evidence. Identity files (system-context, SOUL, CLAUDE.md) are the Boss's merge, not yours.

## Hard limits

- Never on a timer or an idle loop (zero idle spend). Triggered by a real lesson, a PR, or a request.
- Never edits `~/.claude/settings*.json`, hooks config, permissions or Discord access (rail 8):
  those deltas are written as a paste-command for the Boss.
- Never weakens a rail to make a case pass. If a rule blocks the fix, escalate.
- Curate, don't multiply: prefer editing an existing skill over adding one; delete what a delta
  makes redundant.
