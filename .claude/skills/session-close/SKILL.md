---
name: session-close
description: End-of-session ritual — log the session summary to the team brain, persist decisions, update task statuses, and flag unverified claims. Use at the end of every working session, when the Boss says "wrap up" / "done for now" / "good night", or before a long idle.
---

# /session-close — end-of-session ritual

Four steps, in order, every time. Hermes and Frank read the same brain — an
unlogged session is invisible to the rest of the team.

## 1. Log the session summary

`mcp__blimp-brain__log_message(channel=<this session's channel/dm>,
speaker=<your agent key — "duke" or "nanook">, message=<summary>)`

Summary = 3–6 lines: what was worked on, what shipped, what's blocked,
what's next. Plain language (keep-it-simple rule).

## 2. Persist decisions

For each decision that landed this session (pricing, architecture, client,
process), one `mcp__blimp-brain__remember(content=..., tags="decision,...")`
each — if it superseded an earlier decision, recall the old one first and
say `supersedes: <old claim>, as of <date>` in the content. Skip trivia;
decisions and non-obvious discoveries only.

## 3. Update tasks

- `mcp__blimp-brain__tasks(status="in_progress")` — anything finished this
  session → `task_update(status="completed")`; anything stuck →
  `task_update(status="blocked")` with the blocker in the description.
- New work surfaced this session → `task_add` now, not "tomorrow".

## 4. Flag unverified claims (the honesty pass)

List everything reported as "done" this session that lacks an evidence
artifact (a verification command + its output). This includes:

- Hermes completions accepted without the verifier's PASS
  (memory: verify-hermes-claimed-actions)
- Duke's own "should work" statements with no command run
- Anything a restart/cron/external service must confirm later

Format, appended to the step-1 summary or as its own log line:

```
UNVERIFIED: [claim] — needs [exact command or check] — owner [duke/hermes/nanook/logan]
```

Zero unverified claims → say "UNVERIFIED: none" explicitly, so the absence
is an assertion, not an omission.

## 5. (Frank) Keep state.md lean — the <8KB byte-check

Frank's `~/.hermes/profiles/nanook/10-memory/state.md` models memory hygiene, so
it must not bloat (it once hit 39KB — 10+ stacked superseded "LATEST STATE"
blocks). Before exit, run:

```
wc -c < ~/.hermes/profiles/nanook/10-memory/state.md
```

Over **8192 bytes** → do an archive pass: append the superseded material
(everything except the RESUME PROMPT, the single live LATEST STATE / CURRENT
FOCUS, current OPEN LOOPS, the ritual table, and the last ~3 session-log lines)
to `state-archive.md` (**append-only — never delete**, per memory-discipline +
the 2026-06-23 wipe precedent), then trim state.md and refresh the RESUME PROMPT
verbatim so the next session starts from the right place.

## 6. (Discord sessions) tell the Boss

One short Discord message: shipped / blocked / next / unverified count.
No jargon, no wall of text.
